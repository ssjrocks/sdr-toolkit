package com.ssjrocks.sdrtoolkit

import com.ssjrocks.sdrtoolkit.core.Relay
import com.ssjrocks.sdrtoolkit.core.RelayInfo
import com.ssjrocks.sdrtoolkit.core.RtlTcp
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.DataInputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.CopyOnWriteArrayList
import kotlin.concurrent.thread

/**
 * The relay client against a relay that speaks protocol 1 as sdr-tools-relay's server.py does
 * (hello, HMAC login, rtl_tcp), and a few cases (busy, password). RelayLiveTest runs the real relay.
 */
class RelayTest {
    @Test
    fun hmacMatchesPython() {
        // python3 -c "import hmac,hashlib; print(hmac.new(b'pw', b'abc123', hashlib.sha256).hexdigest())"
        assertEquals("c818cc38138f7a4304da66175056a754985a9237a839c6e7748faf4627c915da", Relay.auth("pw", "abc123"))
        assertEquals("", Relay.auth("", "abc123"))
    }

    @Test
    fun commandBytes() {
        assertArrayEquals(byteArrayOf(1, 0x05, 0xF5.toByte(), 0xE1.toByte(), 0x00), RtlTcp.command(RtlTcp.SET_FREQ, 100_000_000))
        assertArrayEquals(byteArrayOf(5, -1, -1, -1, -3), RtlTcp.command(RtlTcp.SET_PPM, -3))
    }

    @Test
    fun addresses() {
        assertEquals("relay.local" to 47320, Relay.parseAddress("relay.local"))
        assertEquals("10.0.0.2" to 1234, Relay.parseAddress(" 10.0.0.2:1234 "))
        assertEquals("fe80::1" to 47320, Relay.parseAddress("[fe80::1]"))
        assertEquals("fe80::1" to 99, Relay.parseAddress("[fe80::1]:99"))
        assertEquals(null, Relay.parseAddress("host:notaport"))
        assertEquals("pc" to 1234, Relay.parseAddress("rtl_tcp://pc:1234/"))
        assertTrue(Relay.isLan("192.168.1.20"))
        assertTrue(Relay.isLan("127.0.0.1"))
        assertFalse(Relay.isLan("100.101.102.103"))     // Tailscale
        assertFalse(Relay.isLan("8.8.8.8"))
    }

    @Test
    fun directoryEntries() {
        val list = Relay.parseDirectory("""{"relays": [
            {"name": "Rooftop", "host": "relay.example.org", "port": 47320, "location": "Sydney",
             "devices": ["Generic RTL2832U OEM"], "password": true, "busy": false, "version": "0.1.4"},
            {"name": "no host"}]}""")
        assertEquals(1, list.size)
        val r = list[0]
        assertEquals("Rooftop", r.name)
        assertEquals("Sydney", r.location)
        assertEquals(listOf("Generic RTL2832U OEM"), r.devices)
        assertTrue(r.password)
        assertEquals(RelayInfo.Origin.INTERNET, r.origin)
    }

    /** A minimal relay: protocol 1 hello, login, then an rtl_tcp header and a few samples. */
    private class FakeRelay(val password: String = "", val busy: Boolean = false) : AutoCloseable {
        val server = ServerSocket(0, 5, InetAddress.getByName("127.0.0.1"))
        val port get() = server.localPort
        val commands = CopyOnWriteArrayList<Pair<Int, Long>>()
        val replies = CopyOnWriteArrayList<JSONObject>()

        init {
            thread(isDaemon = true) {
                while (!server.isClosed) {
                    val s = try { server.accept() } catch (_: Exception) { break }
                    thread(isDaemon = true) {
                        s.use {
                            val out = s.getOutputStream()
                            val nonce = "0123456789abcdef"
                            val hello = JSONObject().put("relay", "sdr-tools-relay").put("protocol", 1)
                                .put("name", "Test relay").put("version", "0.1.4").put("password", password.isNotEmpty())
                                .put("busy", busy).put("nonce", nonce).put("features", listOf("update", "diagnose"))
                                .put("devices", listOf(mapOf("name" to "Generic RTL2832U OEM")))
                            out.write((hello.toString() + "\n").toByteArray())
                            if (busy) return@thread
                            val input = DataInputStream(s.getInputStream())
                            val reply = JSONObject(Relay.readLine(input).toString())
                            replies += reply
                            if (password.isNotEmpty() && reply.optString("auth") != Relay.auth(password, nonce)) {
                                out.write("{\"ok\": false, \"error\": \"wrong password\"}\n".toByteArray())
                                return@thread
                            }
                            if (reply.optBoolean("check")) {
                                out.write("{\"ok\": true, \"checked\": true}\n".toByteArray())
                                return@thread
                            }
                            out.write("{\"ok\": true, \"ppm\": 3}\n".toByteArray())
                            out.write("RTL0".toByteArray() + byteArrayOf(0, 0, 0, 5, 0, 0, 0, 29))
                            out.write(ByteArray(4096) { (it % 256).toByte() })
                            out.flush()
                            try {
                                while (true) {
                                    val cmd = input.readUnsignedByte()
                                    val v = input.readInt().toLong() and 0xFFFFFFFFL
                                    commands += cmd to v
                                }
                            } catch (_: Exception) {
                            }
                        }
                    }
                }
            }
        }

        override fun close() = server.close()
    }

    @Test
    fun loginAndStream() {
        FakeRelay(password = "secret").use { relay ->
            val info = Relay.check("127.0.0.1", relay.port, "secret")
            assertEquals("Test relay", info.name)
            assertEquals(listOf("Generic RTL2832U OEM"), info.devices)
            assertTrue(relay.replies.last().getBoolean("check"))

            val (conn, session) = Relay.connect("127.0.0.1", relay.port, "secret")
            conn.use {
                assertEquals(3, session.answer.getInt("ppm"))
                assertEquals(5, conn.tuner)
                assertEquals("R820T", conn.tunerName)
                assertEquals(29, conn.gainCount)
                val buf = ByteArray(4096)
                var got = 0
                while (got < buf.size) got += conn.read(buf, got, buf.size - got)
                assertEquals(10, buf[10].toInt())
                conn.command(RtlTcp.SET_FREQ, 1_090_000_000)
                conn.command(RtlTcp.SET_GAIN_MODE, 1)
                val end = System.currentTimeMillis() + 2000
                while (relay.commands.size < 2 && System.currentTimeMillis() < end) Thread.sleep(10)
                assertEquals(listOf(1 to 1_090_000_000L, 3 to 1L), relay.commands.toList())
            }
            assertEquals("SDR Toolkit Android", relay.replies.last().getString("client"))
        }
    }

    @Test
    fun wrongOrMissingPassword() {
        FakeRelay(password = "secret").use { relay ->
            try {
                Relay.check("127.0.0.1", relay.port, "")
                fail()
            } catch (e: Relay.NeedsPassword) {
                assertTrue(e.message!!.contains("needs a password"))
            }
            try {
                Relay.check("127.0.0.1", relay.port, "nope")
                fail()
            } catch (e: Relay.RelayException) {
                assertTrue(e.message!!.contains("wrong password"))
            }
        }
    }

    @Test
    fun busyRelay() {
        FakeRelay(busy = true).use { relay ->
            try {
                Relay.openSession("127.0.0.1", relay.port, "")
                fail()
            } catch (e: Relay.Busy) {
                assertTrue(e.message!!.contains("in use"))
            }
        }
    }

    @Test
    fun discovery() {
        val sock = DatagramSocket(0, InetAddress.getByName("127.0.0.1"))
        thread(isDaemon = true) {
            val buf = ByteArray(512)
            val p = DatagramPacket(buf, buf.size)
            sock.receive(p)
            if (String(p.data, 0, p.length).trim() == "SDR-TOOLS-RELAY? 1") {
                val answer = """{"relay": "sdr-tools-relay", "name": "Shack", "port": 47999, "version": "0.1.4",
                    "devices": [{"name": "RTL-SDR Blog V4"}], "password": false, "busy": true}"""
                sock.send(DatagramPacket(answer.toByteArray(), answer.length, p.address, p.port))
            }
        }
        val found = Relay.discover(1000, listOf(InetAddress.getByName("127.0.0.1")), sock.localPort)
        sock.close()
        assertEquals(1, found.size)
        assertEquals("Shack", found[0].name)
        assertEquals(47999, found[0].port)
        assertEquals("127.0.0.1", found[0].host)
        assertTrue(found[0].busy)
        assertEquals(listOf("RTL-SDR Blog V4"), found[0].devices)
    }

    @Test
    fun addressIsRelayOrRtlTcp() {
        // A relay, found to be one by its JSON hello.
        FakeRelay(password = "secret").use { relay ->
            val (conn, session) = RtlTcp.connectAny("127.0.0.1", relay.port, "secret")
            conn.use {
                assertEquals("Test relay", conn.label)
                assertEquals(3, session!!.answer.getInt("ppm"))
                assertEquals(5, conn.tuner)
            }
            try {
                RtlTcp.connectAny("127.0.0.1", relay.port, "")
                fail()
            } catch (e: Relay.NeedsPassword) {
            }
        }
        // A plain rtl_tcp server: its header straight away.
        ServerSocket(0, 5, InetAddress.getByName("127.0.0.1")).use { server ->
            thread(isDaemon = true) {
                server.accept().use { s ->
                    s.getOutputStream().write("RTL0".toByteArray() + byteArrayOf(0, 0, 0, 1, 0, 0, 0, 14) + ByteArray(1000) { 7 })
                    Thread.sleep(500)
                }
            }
            val (conn, session) = RtlTcp.connectAny("127.0.0.1", server.localPort, "")
            conn.use {
                assertEquals(null, session)
                assertEquals("E4000", conn.tunerName)
                assertEquals(14, conn.gainCount)
                val b = ByteArray(10)
                assertEquals(10, conn.read(b))
                assertEquals(7, b[9].toInt())
            }
        }
    }
}
