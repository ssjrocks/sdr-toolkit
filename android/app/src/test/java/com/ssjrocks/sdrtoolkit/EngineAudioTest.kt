package com.ssjrocks.sdrtoolkit

import com.ssjrocks.sdrtoolkit.core.Relay
import com.ssjrocks.sdrtoolkit.dsp.DemodMode
import com.ssjrocks.sdrtoolkit.engine.ConnState
import com.ssjrocks.sdrtoolkit.engine.Source
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowAudioTrack
import java.io.ByteArrayOutputStream
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * What reaches the speaker: the engine tuned to SimRelay's station 600 kHz above the dongle's
 * centre (FM, modulated with an 800 Hz tone) writes about real-time audio to the AudioTrack, with
 * that tone in it; muted, it writes silence.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class EngineAudioTest {
    private val heard = ByteArrayOutputStream()
    private val listener = ShadowAudioTrack.OnAudioDataWrittenListener { _, data, _ -> synchronized(heard) { heard.write(data) } }

    @After
    fun tearDown() {
        ShadowAudioTrack.removeAudioDataListener(listener)
        SdrApp.engine.disconnect()
    }

    private fun samples(): ShortArray = synchronized(heard) {
        val b = heard.toByteArray()
        heard.reset()
        ShortArray(b.size / 2) { ((b[2 * it].toInt() and 0xFF) or (b[2 * it + 1].toInt() shl 8)).toShort() }
    }

    private fun power(x: ShortArray, f: Double): Double {
        var re = 0.0
        var im = 0.0
        for (k in x.indices) {
            re += x[k] * cos(2 * PI * f * k / 48000)
            im -= x[k] * sin(2 * PI * f * k / 48000)
        }
        return (re * re + im * im) / x.size.toDouble() / x.size
    }

    @Test
    fun stationIsHeard() {
        ShadowAudioTrack.addAudioDataListener(listener)
        SimRelay(name = "Audio", discoveryPort = 0).use { relay ->
            val engine = SdrApp.engine
            engine.update {
                it.copy(sampleRate = 2_400_000, tunedHz = 101_100_000, centerHz = 100_500_000, mode = DemodMode.WFM,
                    bandwidth = DemodMode.WFM.defaultBw, volume = 0.7f, muted = false, squelchOn = false)
            }
            val info = Relay.check("127.0.0.1", relay.port, "")
            engine.connect(Source.RelaySource(info.copy(host = "127.0.0.1"), ""))
            val end = System.currentTimeMillis() + 20_000
            while ((engine.spectrum.value?.seq ?: 0) < 50 && System.currentTimeMillis() < end) Thread.sleep(50)
            assertEquals(ConnState.Phase.STREAMING, engine.conn.value.phase)
            Thread.sleep(500)
            samples()                                   // from here on, at the 2.4 MS/s rate
            val t0 = System.nanoTime()
            Thread.sleep(3000)
            val got = samples()
            val seconds = (System.nanoTime() - t0) / 1e9
            println("AUDIO: ${got.size} samples in ${"%.2f".format(seconds)} s")
            // About real time: a track that never starts (or stops being written) shows up here.
            assertTrue("${got.size} samples in $seconds s", got.size > 48000 * seconds * 0.8)
            val tail = got.copyOfRange(got.size / 2, got.size)
            val rms = sqrt(tail.map { it.toDouble() * it }.average()) / 32768
            println("AUDIO: rms ${"%.3f".format(rms)}")
            assertTrue("rms $rms", rms > 0.05)
            val tones = listOf(97.0, 400.0, 800.0, 1000.0, 1200.0).associateWith { power(tail, it) }
            println("AUDIO: tones $tones")
            assertEquals(800.0, tones.maxBy { it.value }.key, 0.0)

            engine.update { it.copy(muted = true) }
            Thread.sleep(500)
            samples()
            Thread.sleep(1000)
            val quiet = samples()
            assertTrue(quiet.isNotEmpty())
            assertTrue("muted, yet ${quiet.maxOf { kotlin.math.abs(it.toInt()) }}", quiet.all { it.toInt() == 0 })
        }
    }
}
