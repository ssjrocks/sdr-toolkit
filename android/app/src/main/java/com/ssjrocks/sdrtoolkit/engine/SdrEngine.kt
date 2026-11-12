package com.ssjrocks.sdrtoolkit.engine

import android.content.Context
import android.util.Log
import com.ssjrocks.sdrtoolkit.adsb.Aircraft
import com.ssjrocks.sdrtoolkit.adsb.ModeSDemod
import com.ssjrocks.sdrtoolkit.adsb.Tracker
import com.ssjrocks.sdrtoolkit.core.Relay
import com.ssjrocks.sdrtoolkit.core.RelayInfo
import com.ssjrocks.sdrtoolkit.core.RtlTcp
import com.ssjrocks.sdrtoolkit.core.RtlTcpConnection
import com.ssjrocks.sdrtoolkit.dsp.DemodMode
import com.ssjrocks.sdrtoolkit.dsp.Demodulator
import com.ssjrocks.sdrtoolkit.dsp.SAMPLE_RATES
import com.ssjrocks.sdrtoolkit.dsp.Spectrum
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.IOException
import java.net.SocketTimeoutException
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.math.abs

/** Where the samples come from. Each ends up as an rtl_tcp stream. */
sealed class Source {
    abstract val label: String

    /** An SDR Tools Relay (found on the LAN or the internet). */
    data class RelaySource(val info: RelayInfo, val password: String) : Source() {
        override val label get() = info.name
    }

    /** An address typed in: a relay or a plain rtl_tcp server, told apart when connecting. */
    data class Address(val name: String, val host: String, val port: Int, val password: String) : Source() {
        override val label get() = name.ifEmpty { "$host:$port" }
    }

    /** A dongle on this phone's USB port, served on localhost by the RTL-SDR driver app. */
    data class Usb(val dongle: String, val port: Int) : Source() {
        override val label get() = dongle
    }
}

enum class Task { RECEIVER, ADSB }

data class ConnState(
    val phase: Phase = Phase.IDLE,
    val source: Source? = null,
    val name: String = "",
    val tuner: String = "",
    val tunerType: Int = 5,
    val isRelay: Boolean = false,
    val onLan: Boolean = true,
    val relayPpm: Int = 0,
    val error: String? = null,
    val needsPassword: Boolean = false,
) {
    enum class Phase { IDLE, CONNECTING, STREAMING }

    val gains: IntArray get() = RtlTcp.gains(tunerType)
}

data class Stats(
    val kbytesPerSec: Float = 0f,
    val dropped: Long = 0,
    val levelDb: Float = -120f,
    val squelchOpen: Boolean = true,
)

class SpectrumFrame(val db: FloatArray, val peak: FloatArray?, val centerHz: Long, val rate: Int,
                    val floorDb: Float, val seq: Long)

data class AdsbStats(val messagesPerSec: Float = 0f, val total: Long = 0, val aircraft: Int = 0)

/**
 * The radio: one source at a time, its settings, and what is made of its samples. A reader
 * thread fills a bounded pool of buffers (old ones are dropped when processing falls behind);
 * the DSP thread turns them into the spectrum, the waterfall, audio and aircraft.
 */
class SdrEngine(private val context: Context) {
    private val store = Store(context)

    private val _settings = MutableStateFlow(store.loadSettings())
    val settings: StateFlow<RxSettings> = _settings.asStateFlow()
    private val _conn = MutableStateFlow(ConnState())
    val conn: StateFlow<ConnState> = _conn.asStateFlow()
    private val _task = MutableStateFlow(Task.RECEIVER)
    val task: StateFlow<Task> = _task.asStateFlow()
    private val _spectrum = MutableStateFlow<SpectrumFrame?>(null)
    val spectrum: StateFlow<SpectrumFrame?> = _spectrum.asStateFlow()
    private val _stats = MutableStateFlow(Stats())
    val stats: StateFlow<Stats> = _stats.asStateFlow()
    private val _aircraft = MutableStateFlow<List<Aircraft>>(emptyList())
    val aircraft: StateFlow<List<Aircraft>> = _aircraft.asStateFlow()
    private val _adsb = MutableStateFlow(AdsbStats())
    val adsb: StateFlow<AdsbStats> = _adsb.asStateFlow()
    private val _sources = MutableStateFlow(store.loadSources())
    val sources: StateFlow<List<SavedSource>> = _sources.asStateFlow()

    @Volatile
    var waterfall = WaterfallBuffer(2048)
        private set

    private val commands = Executors.newSingleThreadExecutor { Thread(it, "sdr-commands") }
    @Volatile
    private var session: Session? = null

    init {
        if (_settings.value.sampleRate !in SAMPLE_RATES) update { it.copy(sampleRate = 2_400_000) }
    }

    // ---- sources -------------------------------------------------------------------------

    fun connect(source: Source) {
        disconnect()
        val s = Session(source)
        session = s
        _conn.value = ConnState(ConnState.Phase.CONNECTING, source, source.label)
        s.start()
    }

    fun disconnect() {
        session?.stop()
        session = null
        if (_conn.value.phase != ConnState.Phase.IDLE || _conn.value.error != null) _conn.value = ConnState()
        _spectrum.value = null
        _stats.value = Stats()
        SdrService.stop(context)
    }

    /** Forgets the last error (after the user has seen it). */
    fun clearError() {
        if (_conn.value.phase == ConnState.Phase.IDLE) _conn.value = ConnState()
    }

    fun saveSource(s: SavedSource) {
        _sources.value = _sources.value.filter { it.key != s.key } + s
        store.saveSources(_sources.value)
    }

    fun removeSource(s: SavedSource) {
        _sources.value = _sources.value.filter { it.key != s.key }
        store.saveSources(_sources.value)
    }

    fun rememberedPassword(key: String) = store.password(key)
    fun rememberPassword(key: String, password: String) = store.setPassword(key, password)

    // ---- settings and tuning -------------------------------------------------------------

    fun update(change: (RxSettings) -> RxSettings) {
        val old = _settings.value
        val new = change(old)
        if (new == old) return
        _settings.value = new
        store.saveSettings(new)
        session?.apply(old, new, false)
    }

    /** Listens on [hz]; the dongle is retuned when it's outside the span, keeping it a quarter span off its DC spike. */
    fun tune(hz: Long) = update { s ->
        val f = hz.coerceIn(MIN_HZ, MAX_HZ)
        var c = s.centerHz
        if (abs(f - c) > s.sampleRate / 2 * 0.9) c = f - s.sampleRate / 4
        s.copy(tunedHz = f, centerHz = c.coerceAtLeast(s.sampleRate / 2L))
    }

    /** Slides the dongle's span along the band; the tuned frequency stays put. */
    fun setCenter(hz: Long) = update { s -> s.copy(centerHz = hz.coerceIn(s.sampleRate / 2L, MAX_HZ)) }

    fun setMode(mode: DemodMode) = update { it.copy(mode = mode, bandwidth = mode.defaultBw) }

    fun setTask(t: Task) {
        if (_task.value == t) return
        _task.value = t
        session?.applyTask()
    }

    fun clearAircraft() {
        session?.clearAircraft = true
        _aircraft.value = emptyList()
    }

    // ---- one connection ------------------------------------------------------------------

    private class Chunk(val data: ByteArray) {
        var len = 0
    }

    private inner class Session(val source: Source) {
        @Volatile
        var running = true
        @Volatile
        var conn: RtlTcpConnection? = null
        @Volatile
        var clearAircraft = false
        private val free = ArrayBlockingQueue<Chunk>(POOL)
        private val full = ArrayBlockingQueue<Chunk>(POOL)
        @Volatile
        private var bytes = 0L
        @Volatile
        private var dropped = 0L

        init {
            repeat(POOL) { free.add(Chunk(ByteArray(CHUNK))) }
        }

        fun start() {
            Thread({ runReader() }, "sdr-reader").start()
        }

        fun stop() {
            running = false
            conn?.close()
        }

        private fun fail(message: String, needsPassword: Boolean = false) {
            if (!running || session !== this) return
            running = false
            conn?.close()
            _conn.value = ConnState(source = source, name = source.label, error = message, needsPassword = needsPassword)
            SdrService.stop(context)
        }

        private fun open(): Pair<RtlTcpConnection, Relay.Session?> = when (source) {
            is Source.RelaySource -> Relay.connect(source.info.host, source.info.port, source.password)
            is Source.Address -> RtlTcp.connectAny(source.host, source.port, source.password)
            is Source.Usb -> {
                // The driver's server takes a moment to start listening after it says it's ready.
                var last: IOException? = null
                var result: RtlTcpConnection? = null
                for (attempt in 0 until 20) {
                    if (!running) throw IOException("cancelled")
                    try {
                        result = RtlTcp.connect("127.0.0.1", source.port, 3000)
                        break
                    } catch (e: IOException) {
                        last = e
                        Thread.sleep(300)
                    }
                }
                (result ?: throw last ?: IOException("the driver didn't start")) to null
            }
        }

        private fun runReader() {
            val (c, relay) = try {
                open()
            } catch (e: Relay.NeedsPassword) {
                fail(e.message ?: "needs a password", needsPassword = true)
                return
            } catch (e: IOException) {
                val msg = e.message ?: e.toString()
                fail(msg, needsPassword = msg.contains("wrong password"))
                return
            } catch (e: InterruptedException) {
                return
            }
            if (!running) {
                c.close()
                return
            }
            conn = c
            c.setTimeout(QUIET_MS)
            val host = when (source) {
                is Source.RelaySource -> source.info.host
                is Source.Address -> source.host
                is Source.Usb -> "127.0.0.1"
            }
            _conn.value = ConnState(
                phase = ConnState.Phase.STREAMING, source = source,
                name = relay?.hello?.optString("name")?.ifEmpty { null } ?: source.label,
                tuner = c.tunerName, tunerType = c.tuner, isRelay = relay != null,
                onLan = relay == null || Relay.isLan(host), relayPpm = relay?.answer?.optInt("ppm", 0) ?: 0,
            )
            apply(null, _settings.value, true)
            SdrService.start(context)
            Thread({ runDsp() }, "sdr-dsp").apply { priority = Thread.MAX_PRIORITY }.start()
            try {
                while (running) {
                    // Processing behind: drop the oldest samples rather than stall the connection.
                    val chunk = free.poll() ?: full.poll()?.also { dropped++ } ?: free.poll(50, TimeUnit.MILLISECONDS)
                    if (chunk == null) continue
                    val n = c.read(chunk.data, 0, chunk.data.size)
                    if (n < 0) {
                        fail("${c.label} closed the connection")
                        break
                    }
                    chunk.len = n
                    bytes += n
                    full.put(chunk)
                }
            } catch (e: SocketTimeoutException) {
                fail("${c.label} sent no samples for ${QUIET_MS / 1000} s: its dongle isn't streaming " +
                    "(unplugged, or in use by another program?)")
            } catch (e: IOException) {
                fail("Lost ${c.label}: ${e.message}")
            } catch (_: InterruptedException) {
            } finally {
                c.close()
            }
        }

        private fun send(cmd: Int, value: Long) {
            val c = conn ?: return
            commands.execute {
                try {
                    c.command(cmd, value)
                } catch (e: IOException) {
                    Log.i(TAG, "command $cmd: ${e.message}")
                }
            }
        }

        /** Sends the commands for what changed from [old] to [new] (everything with [all]). */
        fun apply(old: RxSettings?, new: RxSettings, all: Boolean) {
            if (conn == null) return
            val adsb = _task.value == Task.ADSB
            if (all) {
                if (adsb) applyTask() else {
                    send(RtlTcp.SET_RATE, new.sampleRate.toLong())
                    send(RtlTcp.SET_FREQ, new.centerHz)
                }
            } else if (!adsb && old != null) {
                if (old.sampleRate != new.sampleRate) send(RtlTcp.SET_RATE, new.sampleRate.toLong())
                if (old.centerHz != new.centerHz) send(RtlTcp.SET_FREQ, new.centerHz)
            }
            fun changed(get: (RxSettings) -> Any?) = all || old == null || get(old) != get(new)
            if (changed { it.ppm }) send(RtlTcp.SET_PPM, new.ppm.toLong())
            if (changed { it.rtlAgc }) send(RtlTcp.SET_AGC, if (new.rtlAgc) 1 else 0)
            if (changed { it.directSampling }) send(RtlTcp.SET_DIRECT_SAMPLING, new.directSampling.toLong())
            if (changed { it.offsetTuning } && (new.offsetTuning || !all)) {
                send(RtlTcp.SET_OFFSET_TUNING, if (new.offsetTuning) 1 else 0)
            }
            if (changed { it.biasTee }) send(RtlTcp.SET_BIAS_TEE, if (new.biasTee) 1 else 0)
            if (changed { it.gainAuto to it.gainTenths }) {
                if (new.gainAuto) send(RtlTcp.SET_GAIN_MODE, 0) else {
                    send(RtlTcp.SET_GAIN_MODE, 1)
                    send(RtlTcp.SET_GAIN, new.gainTenths.toLong())
                }
            }
        }

        fun applyTask() {
            val s = _settings.value
            if (_task.value == Task.ADSB) {
                send(RtlTcp.SET_RATE, ADSB_RATE.toLong())
                send(RtlTcp.SET_FREQ, ADSB_FREQ)
            } else {
                send(RtlTcp.SET_RATE, s.sampleRate.toLong())
                send(RtlTcp.SET_FREQ, s.centerHz)
            }
        }

        private fun runDsp() {
            var demod: Demodulator? = null
            var spectrum: Spectrum? = null
            var audio: AudioOut? = null
            val ringSize = 16384
            val ringI = FloatArray(ringSize)
            val ringQ = FloatArray(ringSize)
            var ringPos = 0
            var sinceFrame = 0
            var seq = 0L
            var floor = Float.NaN
            var bi = FloatArray(CHUNK / 2 + 1)
            var bq = FloatArray(CHUNK / 2 + 1)
            val out = FloatArray(CHUNK / 2 + 64)
            var pending = -1            // an I byte whose Q comes in the next chunk
            val tracker = Tracker()
            val modeS = ModeSDemod { m, sig -> tracker.feed(m, sig) }
            var lastStats = System.currentTimeMillis()
            var lastBytes = bytes
            var lastAircraft = 0L
            var lastGood = 0L
            try {
                while (running) {
                    val chunk = full.poll(200, TimeUnit.MILLISECONDS)
                    val now = System.currentTimeMillis()
                    if (chunk != null) {
                        val s = _settings.value
                        if (_task.value == Task.ADSB) {
                            audio?.close()
                            audio = null
                            if (clearAircraft) {
                                tracker.clear()
                                clearAircraft = false
                            }
                            tracker.ttlMs = s.adsbTtlS * 1000L
                            tracker.receiver = if (s.myLat != null && s.myLon != null) s.myLat to s.myLon else null
                            modeS.process(chunk.data, chunk.len)
                            pending = -1
                        } else {
                            // Unsigned 8-bit IQ → -1..1, an I/Q pair split between chunks joined up.
                            var n = 0
                            var i = 0
                            val d = chunk.data
                            if (bi.size < chunk.len / 2 + 1) {
                                bi = FloatArray(chunk.len / 2 + 1); bq = FloatArray(chunk.len / 2 + 1)
                            }
                            if (pending >= 0 && chunk.len > 0) {
                                bi[n] = (pending - 127.5f) / 127.5f
                                bq[n++] = ((d[0].toInt() and 0xFF) - 127.5f) / 127.5f
                                i = 1
                                pending = -1
                            }
                            while (i + 1 < chunk.len) {
                                bi[n] = ((d[i].toInt() and 0xFF) - 127.5f) / 127.5f
                                bq[n++] = ((d[i + 1].toInt() and 0xFF) - 127.5f) / 127.5f
                                i += 2
                            }
                            if (i < chunk.len) pending = d[i].toInt() and 0xFF
                            val rate = s.sampleRate
                            // The spectrum: the newest FFT-size samples, 25 times a second.
                            for (k in 0 until n) {
                                ringI[ringPos] = bi[k]
                                ringQ[ringPos] = bq[k]
                                ringPos = (ringPos + 1) % ringSize
                            }
                            sinceFrame += n
                            val fft = s.fftSize.coerceIn(512, ringSize)
                            if (spectrum == null || spectrum.size != fft) spectrum = Spectrum(fft)
                            val perFrame = rate / FRAMES_PER_SEC
                            if (sinceFrame >= perFrame) {
                                // Keep the remainder, so frames come 25 a second whatever the chunk size.
                                sinceFrame = if (sinceFrame >= 2 * perFrame) 0 else sinceFrame - perFrame
                                val f = spectrum.compute(ringI, ringQ, ringPos, s.averaging, s.peakHold)
                                val sample = FloatArray(128) { f.db[(it * f.db.size) / 128] }
                                sample.sort()
                                val est = sample[sample.size / 5]
                                floor = if (floor.isNaN()) est else floor * 0.9f + est * 0.1f
                                var wf = waterfall
                                val width = minOf(fft, 2048)
                                if (wf.width != width) {
                                    wf = WaterfallBuffer(width)
                                    waterfall = wf
                                }
                                wf.add(f.db, floor - 3, s.waterfallRange.toFloat())
                                _spectrum.value = SpectrumFrame(f.db, f.peak, s.centerHz, rate, floor, ++seq)
                            }
                            // The demodulator, rebuilt when its rate or kind changes.
                            if (demod == null || demod.rate != rate) {
                                demod = Demodulator(rate, s.mode, (s.tunedHz - s.centerHz).toDouble(), s.bandwidth, s.deemphasisUs)
                            }
                            demod.set(s.mode, s.bandwidth, s.deemphasisUs)
                            val offset = (s.tunedHz - s.centerHz).toDouble()
                            if (demod.offset != offset) demod.retune(offset)
                            demod.squelch = if (s.squelchOn) s.squelchDb.toDouble() else null
                            val inSpan = abs(offset) < rate / 2
                            val count = demod.process(bi, bq, n, out)
                            if (audio == null) audio = AudioOut()
                            audio.write(out, count, if (s.muted || !inSpan) 0f else s.volume)
                        }
                        free.offer(chunk)
                    }
                    if (now - lastStats >= 250) {
                        val b = bytes
                        _stats.value = Stats(
                            kbytesPerSec = (b - lastBytes) / ((now - lastStats) / 1000f) / 1024f,
                            dropped = dropped,
                            levelDb = demod?.level?.toFloat() ?: -120f,
                            squelchOpen = demod?.isOpen ?: true,
                        )
                        lastBytes = b
                        lastStats = now
                    }
                    if (_task.value == Task.ADSB && now - lastAircraft >= 1000) {
                        val list = tracker.snapshot()
                        _aircraft.value = list
                        val dt = if (lastAircraft == 0L) 1f else (now - lastAircraft) / 1000f
                        _adsb.value = AdsbStats((tracker.good - lastGood) / dt, tracker.good, list.size)
                        lastGood = tracker.good
                        lastAircraft = now
                    }
                }
            } catch (_: InterruptedException) {
            } catch (e: Exception) {
                Log.e(TAG, "DSP", e)
                fail("Processing failed: $e")
            } finally {
                audio?.close()
            }
        }
    }

    companion object {
        private const val TAG = "SdrEngine"
        const val MIN_HZ = 100_000L
        const val MAX_HZ = 2_200_000_000L
        const val ADSB_RATE = 2_000_000
        const val ADSB_FREQ = 1_090_000_000L
        private const val CHUNK = 65536
        private const val POOL = 48                 // ~0.65 s at 2.4 MS/s
        private const val QUIET_MS = 10_000
        private const val FRAMES_PER_SEC = 25
    }
}
