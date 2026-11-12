package com.ssjrocks.sdrtoolkit.core

import java.io.BufferedInputStream
import java.io.Closeable
import java.io.DataOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.Socket

/**
 * rtl_tcp, the protocol every source speaks: a USB dongle through the Android driver, a relay
 * after its handshake, or any rtl_tcp server. The server sends "RTL0", the tuner type and the
 * number of gain steps (big-endian u32 each), then unsigned 8-bit IQ; the client sends 5-byte
 * commands (a command byte, a big-endian u32). The same as the desktop's receiver/source.py.
 */
object RtlTcp {
    const val SET_FREQ = 1
    const val SET_RATE = 2
    const val SET_GAIN_MODE = 3
    const val SET_GAIN = 4
    const val SET_PPM = 5
    const val SET_IF_GAIN = 6
    const val SET_TEST_MODE = 7
    const val SET_AGC = 8
    const val SET_DIRECT_SAMPLING = 9
    const val SET_OFFSET_TUNING = 10
    const val SET_RTL_XTAL = 11
    const val SET_TUNER_XTAL = 12
    const val SET_GAIN_INDEX = 13
    const val SET_BIAS_TEE = 14

    val TUNER_NAMES = mapOf(0 to "unknown tuner", 1 to "E4000", 2 to "FC0012", 3 to "FC0013", 4 to "FC2580",
        5 to "R820T", 6 to "R828D")

    private val R82XX = intArrayOf(0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297,
        328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496)

    /** Tuner gain steps in tenths of a dB, as librtlsdr lists them for each tuner type. */
    val TUNER_GAINS: Map<Int, IntArray> = mapOf(
        1 to intArrayOf(-10, 15, 40, 65, 90, 115, 140, 165, 190, 215, 240, 290, 340, 420),
        2 to intArrayOf(-99, -40, 71, 179, 192),
        3 to intArrayOf(-99, -73, -65, -63, -60, -58, -54, 58, 61, 63, 65, 67, 68, 70, 71, 179, 181, 182, 184, 186,
            188, 191, 197),
        4 to intArrayOf(0),
        5 to R82XX,
        6 to R82XX,
    )

    fun gains(tuner: Int): IntArray = TUNER_GAINS[tuner] ?: R82XX

    fun command(cmd: Int, value: Long): ByteArray {
        val v = value and 0xFFFFFFFFL
        return byteArrayOf(cmd.toByte(), (v shr 24).toByte(), (v shr 16).toByte(), (v shr 8).toByte(), v.toByte())
    }

    /** Connects to a plain rtl_tcp server. */
    fun connect(host: String, port: Int, timeoutMs: Int = 8000): RtlTcpConnection {
        val sock = Socket()
        try {
            sock.connect(InetSocketAddress(host, port), timeoutMs)
            sock.soTimeout = timeoutMs
        } catch (e: IOException) {
            sock.close()
            throw SourceException("Couldn't reach $host:$port: ${e.message}")
        }
        return RtlTcpConnection.open(sock, "$host:$port")
    }

    /**
     * Connects to an address that is either an SDR Tools Relay or a plain rtl_tcp server, telling
     * them apart by the first byte (a relay's JSON hello, or rtl_tcp's "RTL0"). Returns the stream,
     * and the relay's session if it was one.
     */
    fun connectAny(host: String, port: Int, password: String, timeoutMs: Int = 8000): Pair<RtlTcpConnection, Relay.Session?> {
        val end = System.currentTimeMillis() + 6000
        while (true) {
            val sock = Socket()
            try {
                sock.connect(InetSocketAddress(host, port), timeoutMs)
                sock.soTimeout = timeoutMs
            } catch (e: IOException) {
                sock.close()
                throw SourceException("Couldn't reach $host:$port: ${e.message}")
            }
            try {
                val input = java.io.PushbackInputStream(sock.getInputStream(), 1)
                val first = input.read()
                if (first < 0) throw SourceException("$host:$port closed the connection (busy?)")
                input.unread(first)
                if (first == '{'.code) {
                    val s = Relay.login(sock, input, host, port, password, false)
                    val name = s.hello.optString("name").ifEmpty { "$host:$port" }
                    return RtlTcpConnection.open(sock, name, input) to s
                }
                return RtlTcpConnection.open(sock, "$host:$port", input) to null
            } catch (e: Relay.Busy) {
                sock.close()
                if (System.currentTimeMillis() > end) throw e
                Thread.sleep(500)
            } catch (e: IOException) {
                sock.close()
                throw e
            }
        }
    }
}

class SourceException(message: String) : IOException(message)

/** A connected rtl_tcp stream: header read, commands out, samples in. */
class RtlTcpConnection private constructor(
    private val socket: Socket,
    private val input: InputStream,
    val tuner: Int,
    val gainCount: Int,
    val label: String,
) : Closeable {
    private val out = DataOutputStream(socket.getOutputStream())

    val tunerName: String get() = RtlTcp.TUNER_NAMES[tuner] ?: "tuner $tuner"

    fun command(cmd: Int, value: Long) {
        synchronized(out) {
            out.write(RtlTcp.command(cmd, value))
            out.flush()
        }
    }

    /** Reads samples; returns the number of bytes, or -1 at the end of the stream. */
    fun read(buf: ByteArray, off: Int = 0, len: Int = buf.size): Int = input.read(buf, off, len)

    /** Read timeout in ms (0 = none). */
    fun setTimeout(ms: Int) {
        socket.soTimeout = ms
    }

    override fun close() {
        try {
            socket.close()
        } catch (_: IOException) {
        }
    }

    companion object {
        /**
         * Reads the rtl_tcp header from an already connected (and, for a relay, logged in) socket.
         * [stream] is the socket's input if something has read from it already (unbuffered).
         */
        fun open(socket: Socket, label: String, stream: InputStream? = null): RtlTcpConnection {
            try {
                socket.tcpNoDelay = true
                val input = BufferedInputStream(stream ?: socket.getInputStream(), 1 shl 18)
                val head = ByteArray(12)
                var got = 0
                while (got < 12) {
                    val n = input.read(head, got, 12 - got)
                    if (n < 0) throw SourceException("$label closed the connection (the dongle may be in use or missing)")
                    got += n
                }
                if (String(head, 0, 4, Charsets.US_ASCII) != "RTL0") throw SourceException("$label isn't an rtl_tcp server")
                return RtlTcpConnection(socket, input, u32(head, 4), u32(head, 8), label)
            } catch (e: IOException) {
                socket.close()
                if (e is SourceException) throw e
                throw SourceException("$label: ${e.message}")
            }
        }

        private fun u32(b: ByteArray, o: Int): Int =
            ((b[o].toInt() and 0xFF) shl 24) or ((b[o + 1].toInt() and 0xFF) shl 16) or
                ((b[o + 2].toInt() and 0xFF) shl 8) or (b[o + 3].toInt() and 0xFF)
    }
}
