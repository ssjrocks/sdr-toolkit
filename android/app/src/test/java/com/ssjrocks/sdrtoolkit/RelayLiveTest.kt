package com.ssjrocks.sdrtoolkit

import com.ssjrocks.sdrtoolkit.adsb.ModeSDemod
import com.ssjrocks.sdrtoolkit.adsb.Tracker
import com.ssjrocks.sdrtoolkit.core.Relay
import com.ssjrocks.sdrtoolkit.core.RtlTcp
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket

/**
 * The client against the real SDR Tools Relay (its checkout in SDR_TOOLS_RELAY_SRC, e.g.
 * ../sdr-tools-relay/src), whose dongle plays an ADS-B recording made by AdsbTest's synthesiser:
 * found on the LAN, logged in with the password, tuned, and the aircraft decoded from its samples.
 */
class RelayLiveTest {
    @Test
    fun throughTheRealRelay() {
        val src = System.getenv("SDR_TOOLS_RELAY_SRC")
        assumeTrue("set SDR_TOOLS_RELAY_SRC to sdr-tools-relay's src directory", src != null && File(src).isDirectory)
        val msgs = listOf("8D4840D6202CC371C32CE0576098", "8D40621D58C386435CC412692AD6",
            "8D40621D58C382D690C8AC2863A7", "8D485020994409940838175B284F")
            .map { s -> ByteArray(s.length / 2) { s.substring(it * 2, it * 2 + 2).toInt(16).toByte() } }
        val recording = File.createTempFile("adsb", ".cu8")
        recording.writeBytes(AdsbTest().synthPublic(msgs))
        val port = ServerSocket(0).use { it.localPort }
        val udp = java.net.DatagramSocket(0).use { it.localPort }
        val script = File(javaClass.classLoader!!.getResource("run_relay.py")!!.toURI())
        val proc = ProcessBuilder("python3", script.path, recording.path, "$port", "$udp", "pw")
            .redirectErrorStream(true).apply { environment()["SDR_TOOLS_RELAY_SRC"] = src }.start()
        try {
            val ready = proc.inputStream.bufferedReader().readLine()
            assertEquals("READY", ready)

            val found = Relay.discover(1500, listOf(InetAddress.getByName("127.0.0.1")), udp)
            assertEquals(listOf("Live test relay"), found.map { it.name })
            assertEquals(port, found[0].port)
            assertTrue(found[0].password)

            val info = Relay.check("127.0.0.1", port, "pw")
            assertEquals("live", info.version)

            val tracker = Tracker()
            val demod = ModeSDemod { m, s -> tracker.feed(m, s) }
            val (conn, _) = Relay.connect("127.0.0.1", port, "pw")
            conn.use {
                assertEquals("R820T", conn.tunerName)
                conn.command(RtlTcp.SET_RATE, 2_000_000)
                conn.command(RtlTcp.SET_FREQ, 1_090_000_000)
                conn.command(RtlTcp.SET_GAIN_MODE, 0)
                val buf = ByteArray(65536)
                val end = System.currentTimeMillis() + 8000
                while (System.currentTimeMillis() < end && tracker.snapshot().size < 3) {
                    val n = conn.read(buf)
                    if (n < 0) break
                    demod.process(buf, n)
                }
            }
            val planes = tracker.snapshot().associateBy { it.hex }
            assertEquals("KLM1023", planes["4840D6"]?.callsign)
            assertEquals(52.2572, planes["40621D"]?.lat ?: 0.0, 1e-4)
            assertEquals(159.2, planes["485020"]?.speedKt ?: 0.0, 0.01)
        } finally {
            proc.outputStream.close()
            proc.destroy()
            recording.delete()
        }
    }
}
