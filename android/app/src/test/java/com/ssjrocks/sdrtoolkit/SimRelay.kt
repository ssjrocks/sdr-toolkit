package com.ssjrocks.sdrtoolkit

import com.ssjrocks.sdrtoolkit.core.Relay
import org.json.JSONObject
import java.io.DataInputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.util.Random
import kotlin.concurrent.thread
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * A relay (protocol 1) whose "dongle" makes up a band in real time: a few FM broadcast stations
 * and a narrow-FM carrier that comes and goes, or, on 1090 MHz, aircraft sending ADS-B. It
 * answers LAN scans on [discoveryPort], like SDR Tools Relay.
 */
class SimRelay(val name: String = "Shack PC", val password: String = "",
               discoveryPort: Int = Relay.DISCOVERY_PORT) : AutoCloseable {
    private val server = ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress("0.0.0.0", 0)) }
    val port get() = server.localPort
    private val udp = DatagramSocket(null).apply { reuseAddress = true; broadcast = true; bind(InetSocketAddress(discoveryPort)) }
    @Volatile private var closed = false

    /** The aircraft on 1090 MHz: ICAO, callsign, position, altitude, speed, track. */
    data class Plane(val icao: Int, val callsign: String, val lat: Double, val lon: Double, val alt: Int,
                     val speed: Double, val track: Double, val vr: Int)

    var planes = listOf(
        Plane(0x4840D6, "KLM1023", 52.42, 4.62, 23000, 410.0, 95.0, 1800),
        Plane(0x484CB8, "TRA6PH", 52.21, 4.95, 6000, 230.0, 225.0, -1100),
        Plane(0x3C6444, "DLH4AX", 52.60, 5.10, 37000, 470.0, 270.0, 0),
        Plane(0x400F12, "BAW432", 52.05, 4.30, 31000, 445.0, 300.0, 600),
        Plane(0x4CA2B6, "RYR81LT", 52.33, 4.70, 2500, 160.0, 180.0, -700),
    )

    private fun hello(nonce: String) = JSONObject().put("relay", "sdr-tools-relay").put("protocol", 1)
        .put("name", name).put("version", "0.1.4").put("password", password.isNotEmpty()).put("busy", false)
        .put("nonce", nonce).put("features", org.json.JSONArray().put("check").put("diagnose").put("update"))
        .put("devices", org.json.JSONArray().put(JSONObject().put("name", "RTL-SDR Blog V4")))

    init {
        thread(isDaemon = true, name = "sim-discovery") {
            val buf = ByteArray(512)
            while (!closed) {
                val p = DatagramPacket(buf, buf.size)
                try { udp.receive(p) } catch (_: Exception) { break }
                if (String(p.data, 0, p.length).trim() != "SDR-TOOLS-RELAY? 1") continue
                val a = hello("").apply { remove("nonce"); put("port", port) }.toString().toByteArray()
                try { udp.send(DatagramPacket(a, a.size, p.address, p.port)) } catch (_: Exception) {}
            }
        }
        thread(isDaemon = true, name = "sim-relay") {
            while (!closed) {
                val s = try { server.accept() } catch (_: Exception) { break }
                thread(isDaemon = true) { serve(s) }
            }
        }
    }

    @Volatile var rate = 2_048_000
    @Volatile var freq = 100_000_000L

    private fun serve(s: java.net.Socket) = s.use {
        val out = s.getOutputStream()
        val nonce = "a1b2c3d4e5f60718"
        out.write((hello(nonce).toString() + "\n").toByteArray())
        val input = DataInputStream(s.getInputStream())
        val reply = Relay.readLine(input)
        if (password.isNotEmpty() && reply.optString("auth") != Relay.auth(password, nonce)) {
            out.write("{\"ok\": false, \"error\": \"wrong password\"}\n".toByteArray())
            return
        }
        if (reply.optBoolean("check")) {
            out.write("{\"ok\": true, \"checked\": true}\n".toByteArray())
            return
        }
        out.write("{\"ok\": true, \"ppm\": 0}\n".toByteArray())
        out.write("RTL0".toByteArray() + byteArrayOf(0, 0, 0, 6, 0, 0, 0, 29))   // R828D, as on a Blog V4
        out.flush()
        thread(isDaemon = true) {
            try {
                while (true) {
                    val cmd = input.readUnsignedByte()
                    val v = input.readInt().toLong() and 0xFFFFFFFFL
                    when (cmd) {
                        1 -> freq = v
                        2 -> rate = v.toInt()
                    }
                }
            } catch (_: Exception) {
            }
        }
        var loop: ByteArray? = null
        var loopKey = ""
        var pos = 0
        var t0 = System.nanoTime()
        var sent = 0L
        var sentRate = rate
        try {
            while (!closed) {
                val adsb = freq == 1_090_000_000L
                val key = "$rate:$adsb"
                if (key != loopKey) {
                    loop = if (adsb) adsbLoop() else bandLoop(rate)
                    loopKey = key
                    pos = 0
                    // Like a dongle: samples start now, with no backlog from the time spent making them.
                    t0 = System.nanoTime()
                    sent = 0
                    sentRate = rate
                }
                val due = ((System.nanoTime() - t0) / 1e9 * sentRate * 2).toLong() - sent
                if (due < 16384) {
                    Thread.sleep(5)
                    continue
                }
                val l = loop!!
                val n = minOf(due.toInt() and 1.inv(), l.size - pos, 1 shl 17)
                out.write(l, pos, n)
                pos = (pos + n) % l.size
                sent += n
            }
        } catch (_: Exception) {
        }
    }

    /** One second of band: FM stations, a narrow-FM carrier on for half the time, noise. */
    private fun bandLoop(rate: Int): ByteArray {
        val rnd = Random(5)
        val n = rate
        val out = ByteArray(n * 2)
        data class Stn(val off: Double, val amp: Double, val dev: Double, val tone: Double, val on: (Double) -> Boolean)
        val stations = listOf(
            Stn(-900e3, 0.10, 60e3, 1000.0) { true },
            Stn(-300e3, 0.30, 70e3, 400.0) { true },
            Stn(600e3, 0.22, 65e3, 800.0) { true },
            Stn(1000e3, 0.05, 50e3, 1200.0) { true },
            Stn(160e3, 0.06, 2.5e3, 700.0) { t -> t % 1.0 < 0.5 },
        )
        val phase = DoubleArray(stations.size)
        for (k in 0 until n) {
            val t = k.toDouble() / rate
            var i = rnd.nextGaussian() * 0.018
            var q = rnd.nextGaussian() * 0.018
            for ((j, st) in stations.withIndex()) {
                val inst = st.off + st.dev * sin(2 * PI * st.tone * t) * 0.7 + st.dev * 0.3 * sin(2 * PI * 97 * t)
                phase[j] += 2 * PI * inst / rate
                if (!st.on(t)) continue
                i += st.amp * cos(phase[j])
                q += st.amp * sin(phase[j])
            }
            out[2 * k] = (127.5 + 127.5 * i).toInt().coerceIn(0, 255).toByte()
            out[2 * k + 1] = (127.5 + 127.5 * q).toInt().coerceIn(0, 255).toByte()
        }
        return out
    }

    /** One second at 2 MS/s with each aircraft's identity, even and odd positions and velocity. */
    private fun adsbLoop(): ByteArray {
        val rnd = Random(9)
        val msgs = planes.flatMap { p ->
            listOf(AdsbEncoder.identification(p.icao, p.callsign),
                AdsbEncoder.position(p.icao, p.lat, p.lon, p.alt, odd = false),
                AdsbEncoder.velocity(p.icao, p.speed, p.track, p.vr),
                AdsbEncoder.position(p.icao, p.lat, p.lon, p.alt, odd = true)).map { it to p }
        }.shuffled(rnd)
        val n = 2_000_000
        val iq = FloatArray(n * 2)
        for (k in 0 until n * 2) iq[k] = (rnd.nextGaussian() * 2.5).toFloat()
        var at = 1000
        for ((m, p) in msgs) {
            val amp = 18.0 + (p.icao % 40)
            val ph = rnd.nextDouble() * 2 * PI
            val pulses = BooleanArray(16 + 224)
            for (x in intArrayOf(0, 2, 7, 9)) pulses[x] = true
            for (b in 0 until 112) {
                val bit = (m[b / 8].toInt() ushr (7 - b % 8)) and 1
                pulses[16 + 2 * b + (1 - bit)] = true
            }
            for ((x, on) in pulses.withIndex()) if (on) {
                iq[2 * (at + x)] += (amp * cos(ph)).toFloat()
                iq[2 * (at + x) + 1] += (amp * sin(ph)).toFloat()
            }
            at += n / (msgs.size + 1)
        }
        return ByteArray(n * 2) { (127.5 + iq[it]).toInt().coerceIn(0, 255).toByte() }
    }

    override fun close() {
        closed = true
        server.close()
        udp.close()
    }
}
