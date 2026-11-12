package com.ssjrocks.sdrtoolkit.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.URL
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * SDR Tools Relay (github.com/ssjrocks/sdr-tools-relay): an RTL-SDR on another PC.
 *
 * Protocol version 1, as the desktop's relay.py speaks it: the relay sends a JSON hello with a
 * nonce, the client answers with HMAC-SHA256(password, nonce), the relay says {"ok": true} and
 * from then on speaks plain rtl_tcp. LAN discovery: "SDR-TOOLS-RELAY? 1" to UDP 47321 (broadcast),
 * each relay answers with its hello plus "port".
 */
object Relay {
    const val PORT = 47320
    const val DISCOVERY_PORT = 47321
    val DISCOVER = "SDR-TOOLS-RELAY? 1".toByteArray(Charsets.US_ASCII)
    const val DIRECTORY = "https://ssjrocks.com/api/sdr-relay/v1/relays"
    const val CLIENT = "SDR Toolkit Android"
    var clientVersion = "0.1.0"
    private const val BUSY_RETRY_MS = 6000L

    open class RelayException(message: String) : IOException(message)
    class Busy(message: String) : RelayException(message)
    class NeedsPassword(message: String) : RelayException(message)

    fun auth(password: String, nonce: String): String {
        if (password.isEmpty()) return ""
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(password.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        return mac.doFinal(nonce.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }

    /** One JSON line, read a byte at a time so nothing after the newline is consumed. */
    internal fun readLine(input: InputStream, limit: Int = 8192): JSONObject {
        val buf = ByteArrayOutputStream()
        while (true) {
            val b = input.read()
            if (b < 0) throw RelayException("the relay closed the connection")
            if (b == '\n'.code) break
            buf.write(b)
            if (buf.size() > limit) throw RelayException("that isn't an SDR Tools Relay")
        }
        return try {
            JSONObject(buf.toString(Charsets.UTF_8.name()))
        } catch (e: Exception) {
            throw RelayException("that isn't an SDR Tools Relay")
        }
    }

    private fun open(host: String, port: Int, timeoutMs: Int): Socket {
        val sock = Socket()
        try {
            sock.connect(InetSocketAddress(host, port), timeoutMs)
            sock.soTimeout = timeoutMs
        } catch (e: IOException) {
            sock.close()
            throw RelayException("Couldn't reach $host:$port: ${e.message}")
        }
        return sock
    }

    class Session(val socket: Socket, val hello: JSONObject, val answer: JSONObject)

    /**
     * Connects and logs in. With [check], only the password is checked: the relay doesn't open its
     * dongle (relays before 0.1.2 ignore it and open it).
     */
    fun openSession(host: String, port: Int, password: String, check: Boolean = false,
                    timeoutMs: Int = 8000): Session {
        return login(open(host, port, timeoutMs), null, host, port, password, check)
    }

    /** Logs in on a connected socket ([stream]: its input, if something has peeked at it). */
    fun login(sock: Socket, stream: InputStream?, host: String, port: Int, password: String, check: Boolean): Session {
        try {
            val input = stream ?: sock.getInputStream()
            val hello = readLine(input)
            if (hello.optString("relay") != "sdr-tools-relay") throw RelayException("that isn't an SDR Tools Relay")
            val name = hello.optString("name").ifEmpty { "The relay" }
            if (hello.optBoolean("busy")) throw Busy("$name is in use by someone else")
            if (hello.optBoolean("password") && password.isEmpty()) throw NeedsPassword("$name needs a password")
            val reply = JSONObject()
                .put("client", CLIENT)
                .put("version", clientVersion)
                .put("check", check)
                .put("auth", auth(password, hello.optString("nonce")))
            sock.getOutputStream().apply {
                write((reply.toString() + "\n").toByteArray(Charsets.UTF_8))
                flush()
            }
            val answer = readLine(input)
            if (!answer.optBoolean("ok")) {
                val error = answer.optString("error").ifEmpty { "refused" }
                if (error == "busy") throw Busy("$name is in use by someone else")
                throw RelayException("$name: $error")
            }
            return Session(sock, hello, answer)
        } catch (e: IOException) {
            sock.close()
            if (e is RelayException) throw e
            throw RelayException("$host:$port: ${e.message}")
        }
    }

    /** Checks the password without opening the relay's dongle; returns the relay's details. */
    fun check(host: String, port: Int, password: String): RelayInfo {
        val s = openSession(host, port, password, check = true)
        s.socket.close()
        return RelayInfo.fromHello(s.hello, host, port, RelayInfo.Origin.ADDRESS)
            .copy(ppm = s.answer.optInt("ppm", 0))
    }

    /**
     * Logs in and returns the rtl_tcp stream. "In use" straight after a previous connection is
     * usually that connection still closing the dongle, so it tries again for a few seconds.
     */
    fun connect(host: String, port: Int, password: String): Pair<RtlTcpConnection, Session> {
        val end = System.currentTimeMillis() + BUSY_RETRY_MS
        while (true) {
            try {
                val s = openSession(host, port, password)
                val name = s.hello.optString("name").ifEmpty { "$host:$port" }
                return RtlTcpConnection.open(s.socket, name) to s
            } catch (e: Busy) {
                if (System.currentTimeMillis() > end) throw e
                Thread.sleep(500)
            }
        }
    }

    /** The relay's hello (in use or not, its version), without logging in. */
    fun hello(host: String, port: Int, timeoutMs: Int = 5000): JSONObject {
        open(host, port, timeoutMs).use { return readLine(it.getInputStream()) }
    }

    // ---- finding relays ---------------------------------------------------

    /** 255.255.255.255 and each IPv4 interface's broadcast address. */
    fun broadcastAddresses(): List<InetAddress> {
        val out = linkedSetOf<InetAddress>(InetAddress.getByName("255.255.255.255"))
        try {
            for (iface in NetworkInterface.getNetworkInterfaces()) {
                if (!iface.isUp || iface.isLoopback) continue
                for (a in iface.interfaceAddresses) {
                    if (a.address is Inet4Address && a.networkPrefixLength < 31) a.broadcast?.let { out.add(it) }
                }
            }
        } catch (_: Exception) {
        }
        return out.toList()
    }

    /** Relays on the LAN, by UDP broadcast. */
    fun discover(timeoutMs: Int = 1500, targets: List<InetAddress>? = null, port: Int = DISCOVERY_PORT): List<RelayInfo> {
        val found = linkedMapOf<String, RelayInfo>()
        DatagramSocket().use { s ->
            s.broadcast = true
            for (addr in targets ?: broadcastAddresses()) {
                try {
                    s.send(DatagramPacket(DISCOVER, DISCOVER.size, addr, port))
                } catch (_: IOException) {
                }
            }
            val end = System.currentTimeMillis() + timeoutMs
            val buf = ByteArray(8192)
            while (true) {
                val left = end - System.currentTimeMillis()
                if (left <= 0) break
                s.soTimeout = left.toInt().coerceAtLeast(1)
                val p = DatagramPacket(buf, buf.size)
                try {
                    s.receive(p)
                } catch (_: SocketTimeoutException) {
                    break
                }
                val info = try {
                    JSONObject(String(p.data, 0, p.length, Charsets.UTF_8))
                } catch (_: Exception) {
                    continue
                }
                if (info.optString("relay") != "sdr-tools-relay") continue
                val host = p.address.hostAddress ?: continue
                val r = RelayInfo.fromHello(info, host, info.optInt("port", PORT), RelayInfo.Origin.LAN)
                found["${r.host}:${r.port}"] = r
            }
        }
        return found.values.sortedBy { it.name.lowercase() }
    }

    /** Relays listed on ssjrocks.com. */
    fun directory(url: String = DIRECTORY, timeoutMs: Int = 10000): List<RelayInfo> {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = timeoutMs
        c.readTimeout = timeoutMs
        c.setRequestProperty("User-Agent", "SDR-Toolkit-Android/$clientVersion (+https://github.com/ssjrocks/sdr-toolkit)")
        try {
            if (c.responseCode != 200) throw RelayException("the relay list answered ${c.responseCode}")
            val body = c.inputStream.readBytes().toString(Charsets.UTF_8)
            return parseDirectory(body)
        } finally {
            c.disconnect()
        }
    }

    fun parseDirectory(body: String): List<RelayInfo> {
        val list = JSONObject(body).optJSONArray("relays") ?: JSONArray()
        return (0 until list.length()).mapNotNull { i ->
            val o = list.optJSONObject(i) ?: return@mapNotNull null
            val host = o.optString("host")
            if (host.isEmpty()) null else RelayInfo.fromHello(o, host, o.optInt("port", PORT), RelayInfo.Origin.INTERNET)
        }
    }

    /** "host", "host:port", "[v6]:port" → host and port (default 47320). */
    fun parseAddress(text: String, defaultPort: Int = PORT): Pair<String, Int>? {
        val t = text.trim().removePrefix("rtl_tcp://").removePrefix("tcp://").trimEnd('/')
        if (t.isEmpty()) return null
        if (t.startsWith("[")) {
            val close = t.indexOf(']')
            if (close < 0) return null
            val host = t.substring(1, close)
            val port = t.substring(close + 1).removePrefix(":").toIntOrNull() ?: defaultPort
            return host to port
        }
        if (t.count { it == ':' } == 1) {
            val (h, p) = t.split(":")
            val port = p.toIntOrNull() ?: return null
            if (port !in 1..65535 || h.isEmpty()) return null
            return h to port
        }
        return t to defaultPort
    }

    /**
     * The relay is on this network (a private LAN address, or this phone), so at your location.
     * Tailscale (100.64.0.0/10) and internet addresses could be anywhere.
     */
    fun isLan(host: String): Boolean {
        val ip = try {
            InetAddress.getByName(host)
        } catch (_: Exception) {
            return false
        }
        val b = ip.address
        if (ip is Inet4Address && (b[0].toInt() and 0xFF) == 100 && (b[1].toInt() and 0xC0) == 64) return false
        return ip.isSiteLocalAddress || ip.isLoopbackAddress || ip.isLinkLocalAddress
    }
}

/** A relay as found on the LAN, listed on the internet or typed in. */
data class RelayInfo(
    val name: String,
    val host: String,
    val port: Int,
    val version: String = "",
    val devices: List<String> = emptyList(),
    val password: Boolean = false,
    val busy: Boolean = false,
    val features: List<String> = emptyList(),
    val location: String = "",
    val origin: Origin = Origin.ADDRESS,
    val ppm: Int = 0,
) {
    enum class Origin { LAN, INTERNET, ADDRESS }

    val address: String get() = if (host.contains(':')) "[$host]:$port" else "$host:$port"

    companion object {
        fun fromHello(o: JSONObject, host: String, port: Int, origin: Origin): RelayInfo {
            val devs = o.optJSONArray("devices")
            val devices = (0 until (devs?.length() ?: 0)).mapNotNull { i ->
                when (val d = devs!!.opt(i)) {
                    is JSONObject -> d.optString("name").ifEmpty { null }
                    is String -> d.ifEmpty { null }
                    else -> null
                }
            }
            val feats = o.optJSONArray("features")
            return RelayInfo(
                name = o.optString("name").ifEmpty { host },
                host = host,
                port = port,
                version = o.optString("version"),
                devices = devices,
                password = o.optBoolean("password"),
                busy = o.optBoolean("busy"),
                features = (0 until (feats?.length() ?: 0)).map { feats!!.optString(it) },
                location = o.optString("location"),
                origin = origin,
            )
        }
    }
}
