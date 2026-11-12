package com.ssjrocks.sdrtoolkit.adsb

import kotlin.math.log10
import kotlin.math.sqrt

/**
 * Mode S / ADS-B on 1090 MHz from 2 MS/s unsigned 8-bit IQ: the magnitude of each sample, a
 * search for the preamble (pulses at 0, 1, 3.5 and 4.5 µs), then pulse-position bits (a pulse in
 * the first or second half of each microsecond), checked with the Mode S CRC.
 */
/** [onMessage] gets each candidate message and says whether it was a good one. */
class ModeSDemod(private val onMessage: (msg: ByteArray, signalDbfs: Double) -> Boolean) {
    private var mag = IntArray(1 shl 16)
    private var have = 0          // samples in mag (the first CARRY are from the previous block)
    private var start = 0         // where to resume the search

    var preambles = 0L
        private set
    var messages = 0L
        private set

    private var odd = -1          // an I byte whose Q is in the next block

    /** Unsigned 8-bit IQ, interleaved (any number of bytes: a pair split between blocks is joined). */
    fun process(iq: ByteArray, len: Int) {
        if (mag.size < have + len / 2 + 1) mag = mag.copyOf(have + len / 2 + 4096)
        var k = have
        var i = 0
        if (odd >= 0 && len > 0) {
            mag[k++] = MAG[(odd shl 8) or (iq[0].toInt() and 0xFF)].code
            odd = -1
            i = 1
        }
        while (i + 1 < len) {
            mag[k++] = MAG[((iq[i].toInt() and 0xFF) shl 8) or (iq[i + 1].toInt() and 0xFF)].code
            i += 2
        }
        if (i < len) odd = iq[i].toInt() and 0xFF
        have = k
        val end = have - FULL
        var j = start.coerceAtMost(have)
        val bits = ByteArray(14)
        val m = mag
        while (j < end) {
            val m0 = m[j]
            // The preamble's shape (dump1090's test): highs at 0, 2, 7, 9; lows between.
            if (!(m0 > m[j + 1] && m[j + 1] < m[j + 2] && m[j + 2] > m[j + 3] && m[j + 3] < m0 &&
                    m[j + 4] < m0 && m[j + 5] < m0 && m[j + 6] < m0 && m[j + 7] > m[j + 8] &&
                    m[j + 8] < m[j + 9] && m[j + 9] > m[j + 6])) {
                j++
                continue
            }
            val high = (m0 + m[j + 2] + m[j + 7] + m[j + 9]) / 6
            if (m[j + 4] >= high || m[j + 5] >= high ||
                m[j + 11] >= high || m[j + 12] >= high || m[j + 13] >= high || m[j + 14] >= high) {
                j++
                continue
            }
            preambles++
            java.util.Arrays.fill(bits, 0)
            var weak = 0
            var prev = 0
            for (b in 0 until LONG_BITS) {
                val lo = m[j + PREAMBLE + 2 * b]
                val hi = m[j + PREAMBLE + 2 * b + 1]
                val bit = when {
                    lo > hi -> 1
                    lo < hi -> 0
                    else -> prev
                }
                val delta = if (lo > hi) lo - hi else hi - lo
                if (b < SHORT_BITS && delta < high / 8) weak++
                prev = bit
                if (bit == 1) bits[b / 8] = (bits[b / 8].toInt() or (0x80 ushr (b % 8))).toByte()
            }
            if (weak > 10) {
                j++
                continue
            }
            val df = (bits[0].toInt() and 0xFF) ushr 3
            val n = if (df >= 16) 14 else 7
            val msg = bits.copyOf(n)
            val signal = (m0 + m[j + 2] + m[j + 7] + m[j + 9]) / 4.0 / FULL_SCALE
            if (onMessage(msg, 20 * log10(signal.coerceAtLeast(1e-6)))) {
                messages++
                j += PREAMBLE + n * 16          // no preambles inside a good message
            } else {
                j++
            }
        }
        // Keep the tail: preambles that start there need the next block's samples.
        val tail = have - j
        System.arraycopy(m, j, m, 0, tail)
        have = tail
        start = 0
    }

    companion object {
        const val PREAMBLE = 16          // samples (8 µs)
        const val LONG_BITS = 112
        const val SHORT_BITS = 56
        const val FULL = PREAMBLE + LONG_BITS * 2
        const val FULL_SCALE = 127.5 * 360

        /** sqrt(I² + Q²) for every pair of bytes, scaled to 0..65535. */
        val MAG: CharArray = CharArray(65536) { v ->
            val i = (v ushr 8) - 127.5
            val q = (v and 0xFF) - 127.5
            (sqrt(i * i + q * q) * 360).toInt().coerceAtMost(65535).toChar()
        }
    }
}

/** The Mode S CRC (polynomial 0xFFF409), and single-bit repair for 112-bit messages. */
object Crc {
    private val TABLE = IntArray(256) { i ->
        var c = i shl 16
        repeat(8) { c = if (c and 0x800000 != 0) (c shl 1) xor 0xFFF409 else c shl 1 }
        c and 0xFFFFFF
    }

    /** CRC of everything before the last 3 bytes, XORed with them: 0 for a good DF11/17/18, the address for others. */
    fun residual(msg: ByteArray): Int {
        val n = msg.size - 3
        var c = 0
        for (i in 0 until n) c = ((c shl 8) xor TABLE[((c ushr 16) xor (msg[i].toInt() and 0xFF)) and 0xFF]) and 0xFFFFFF
        val parity = ((msg[n].toInt() and 0xFF) shl 16) or ((msg[n + 1].toInt() and 0xFF) shl 8) or (msg[n + 2].toInt() and 0xFF)
        return c xor parity
    }

    private val SYNDROME_112: Map<Int, Int> by lazy {
        val out = HashMap<Int, Int>()
        for (bit in 5 until 112) {        // never the DF
            val m = ByteArray(14)
            m[bit / 8] = (0x80 ushr (bit % 8)).toByte()
            out[residual(m)] = bit
        }
        out
    }

    /** Repairs one wrong bit of a 112-bit DF17/18 message in place; true if it's good now. */
    fun fix112(msg: ByteArray): Boolean {
        val r = residual(msg)
        if (r == 0) return true
        val bit = SYNDROME_112[r] ?: return false
        msg[bit / 8] = (msg[bit / 8].toInt() xor (0x80 ushr (bit % 8))).toByte()
        return true
    }
}

/** What a message says, decoded. */
sealed class ModeSMessage(val icao: Int) {
    class Identification(icao: Int, val callsign: String, val category: Int) : ModeSMessage(icao)
    class Position(icao: Int, val altitudeFt: Int?, val odd: Boolean, val latCpr: Int, val lonCpr: Int,
                   val gnss: Boolean) : ModeSMessage(icao)
    class Surface(icao: Int, val groundSpeedKt: Double?, val track: Double?) : ModeSMessage(icao)
    class Velocity(icao: Int, val speedKt: Double?, val track: Double?, val verticalRateFpm: Int?,
                   val airspeed: Boolean) : ModeSMessage(icao)
    class Altitude(icao: Int, val altitudeFt: Int?) : ModeSMessage(icao)
    class Squawk(icao: Int, val squawk: String) : ModeSMessage(icao)
    class Seen(icao: Int) : ModeSMessage(icao)
}

object ModeSDecoder {
    private const val CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"

    /** Bits [from]..[to] of the message, 1-based and inclusive, as Mode S documents number them. */
    fun bits(msg: ByteArray, from: Int, to: Int): Long {
        var v = 0L
        for (b in from..to) {
            val i = b - 1
            v = (v shl 1) or (((msg[i / 8].toInt() ushr (7 - i % 8)) and 1).toLong())
        }
        return v
    }

    fun df(msg: ByteArray) = (msg[0].toInt() and 0xFF) ushr 3

    fun icaoOf(msg: ByteArray): Int =
        ((msg[1].toInt() and 0xFF) shl 16) or ((msg[2].toInt() and 0xFF) shl 8) or (msg[3].toInt() and 0xFF)

    /**
     * Checks and decodes a message. [known] says whether an address has been seen in a message with
     * its own CRC (DF11/17/18) lately: other formats carry the address in their CRC, so they're only
     * believed for aircraft already heard.
     */
    fun decode(msg: ByteArray, known: (Int) -> Boolean): ModeSMessage? {
        val df = df(msg)
        return when (df) {
            17, 18 -> {
                if (msg.size != 14 || !Crc.fix112(msg)) return null
                if (df == 18 && bits(msg, 6, 8).toInt() !in listOf(0, 1, 6)) return null   // TIS-B with other addressing
                extended(icaoOf(msg), msg)
            }
            11 -> {
                if (msg.size != 7) return null
                val r = Crc.residual(msg)
                if (r and 0x7F.inv() != 0) return null        // the remainder is the interrogator code (< 80)
                ModeSMessage.Seen(icaoOf(msg))
            }
            0, 4, 16, 20 -> {
                if ((df >= 16) != (msg.size == 14)) return null
                val icao = Crc.residual(msg)
                if (!known(icao)) return null
                ModeSMessage.Altitude(icao, altitude13(bits(msg, 20, 32).toInt()))
            }
            5, 21 -> {
                if ((df >= 16) != (msg.size == 14)) return null
                val icao = Crc.residual(msg)
                if (!known(icao)) return null
                ModeSMessage.Squawk(icao, squawk(bits(msg, 20, 32).toInt()))
            }
            else -> null
        }
    }

    private fun extended(icao: Int, msg: ByteArray): ModeSMessage {
        // ME is bits 33-88; ME bit k is message bit k + 32.
        fun me(from: Int, to: Int) = bits(msg, from + 32, to + 32)
        val tc = me(1, 5).toInt()
        return when (tc) {
            in 1..4 -> {
                val sb = StringBuilder()
                for (c in 0 until 8) sb.append(CHARS[me(9 + c * 6, 14 + c * 6).toInt()])
                ModeSMessage.Identification(icao, sb.toString().replace("#", "").trim(), me(6, 8).toInt())
            }
            in 9..18, in 20..22 -> {
                val altCode = me(9, 20).toInt()
                val alt = if (tc <= 18) altitude12(altCode) else if (altCode != 0) (altCode * 3.28084).toInt() else null
                ModeSMessage.Position(icao, alt, me(22, 22) == 1L, me(23, 39).toInt(), me(40, 56).toInt(), tc >= 20)
            }
            in 5..8 -> {
                val mov = me(6, 12).toInt()
                val speed = when {
                    mov == 1 -> 0.0
                    mov in 2..8 -> 0.125 + (mov - 2) * 0.125
                    mov in 9..12 -> 1.0 + (mov - 9) * 0.25
                    mov in 13..38 -> 2.0 + (mov - 13) * 0.5
                    mov in 39..93 -> 15.0 + (mov - 39) * 1.0
                    mov in 94..108 -> 70.0 + (mov - 94) * 2.0
                    mov in 109..123 -> 100.0 + (mov - 109) * 5.0
                    mov == 124 -> 175.0
                    else -> null
                }
                val track = if (me(13, 13) == 1L) me(14, 20) * 360.0 / 128 else null
                ModeSMessage.Surface(icao, speed, track)
            }
            19 -> velocity(icao, msg, ::me)
            else -> ModeSMessage.Seen(icao)
        }
    }

    private fun velocity(icao: Int, msg: ByteArray, me: (Int, Int) -> Long): ModeSMessage {
        val st = me(6, 8).toInt()
        val vrRaw = me(38, 46).toInt()
        val vr = if (vrRaw == 0) null else (vrRaw - 1) * 64 * (if (me(37, 37) == 1L) -1 else 1)
        return when (st) {
            1, 2 -> {
                val ewRaw = me(15, 24).toInt()
                val nsRaw = me(26, 35).toInt()
                if (ewRaw == 0 || nsRaw == 0) return ModeSMessage.Velocity(icao, null, null, vr, false)
                val mult = if (st == 2) 4 else 1
                val vx = (ewRaw - 1) * mult * (if (me(14, 14) == 1L) -1.0 else 1.0)
                val vy = (nsRaw - 1) * mult * (if (me(25, 25) == 1L) -1.0 else 1.0)
                val speed = sqrt(vx * vx + vy * vy)
                var track = Math.toDegrees(kotlin.math.atan2(vx, vy))
                if (track < 0) track += 360.0
                ModeSMessage.Velocity(icao, speed, track, vr, false)
            }
            3, 4 -> {
                val heading = if (me(14, 14) == 1L) me(15, 24) * 360.0 / 1024 else null
                val asRaw = me(26, 35).toInt()
                val speed = if (asRaw == 0) null else ((asRaw - 1) * (if (st == 4) 4 else 1)).toDouble()
                ModeSMessage.Velocity(icao, speed, heading, vr, true)
            }
            else -> ModeSMessage.Seen(icao)
        }
    }

    /** ADS-B's 12-bit altitude (the Q bit set: 25 ft steps). Gillham (100 ft) isn't decoded. */
    fun altitude12(code: Int): Int? {
        if (code == 0) return null
        if (code and 0x10 == 0) return null
        val n = ((code and 0xFE0) ushr 1) or (code and 0x0F)
        return n * 25 - 1000
    }

    /** The 13-bit altitude code of DF0/4/16/20 (M and Q bits). */
    fun altitude13(code: Int): Int? {
        if (code == 0) return null
        if (code and 0x40 != 0) return null             // M: metres, rare
        if (code and 0x10 == 0) return null             // Gillham
        val n = ((code and 0x1F80) ushr 2) or ((code and 0x20) ushr 1) or (code and 0x0F)
        return n * 25 - 1000
    }

    /** The 13-bit identity code (C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4) as four octal digits. */
    fun squawk(id: Int): String {
        fun b(pos: Int) = (id ushr (12 - pos)) and 1     // pos 0 = C1 ... 12 = D4
        val a = b(5) * 4 + b(3) * 2 + b(1)
        val bb = b(11) * 4 + b(9) * 2 + b(7)
        val c = b(4) * 4 + b(2) * 2 + b(0)
        val d = b(12) * 4 + b(10) * 2 + b(8)
        return "$a$bb$c$d"
    }
}
