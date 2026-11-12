package com.ssjrocks.sdrtoolkit

import com.ssjrocks.sdrtoolkit.adsb.Cpr
import com.ssjrocks.sdrtoolkit.adsb.Crc
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.roundToInt
import kotlin.math.sin

/** DF17 extended squitters built from the standard (DO-260), for tests: identity, position, velocity. */
object AdsbEncoder {
    private const val CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"

    private class Bits {
        val b = ByteArray(14)
        /** Sets message bits [from]..[from + width - 1] (1-based) to [v]. */
        fun put(from: Int, width: Int, v: Long) {
            for (i in 0 until width) {
                val bit = ((v ushr (width - 1 - i)) and 1L).toInt()
                val pos = from - 1 + i
                if (bit == 1) b[pos / 8] = (b[pos / 8].toInt() or (0x80 ushr (pos % 8))).toByte()
            }
        }
        fun me(from: Int, width: Int, v: Long) = put(from + 32, width, v)
        fun finish(): ByteArray {
            val crc = Crc.residual(b)       // with zero parity, the residual is the CRC
            b[11] = (crc ushr 16).toByte(); b[12] = (crc ushr 8).toByte(); b[13] = crc.toByte()
            return b
        }
    }

    private fun header(icao: Int) = Bits().apply {
        put(1, 5, 17)
        put(6, 3, 5)
        put(9, 24, icao.toLong())
    }

    fun identification(icao: Int, callsign: String): ByteArray = header(icao).apply {
        me(1, 5, 4)
        me(6, 3, 3)
        val cs = callsign.padEnd(8).take(8)
        for ((i, c) in cs.withIndex()) me(9 + i * 6, 6, CHARS.indexOf(c).let { if (it < 0) 32 else it }.toLong())
    }.finish()

    private fun mod(a: Double, b: Double) = ((a % b) + b) % b

    fun position(icao: Int, lat: Double, lon: Double, altitudeFt: Int, odd: Boolean): ByteArray = header(icao).apply {
        me(1, 5, 11)
        val n = (altitudeFt + 1000) / 25
        me(9, 12, (((n ushr 4) shl 5) or 0x10 or (n and 0xF)).toLong())
        val f = if (odd) 1 else 0
        me(22, 1, f.toLong())
        val dLat = 360.0 / (60 - f)
        val yz = floor(131072 * mod(lat, dLat) / dLat + 0.5)
        val rlat = dLat * (yz / 131072 + floor(lat / dLat))
        val dLon = 360.0 / maxOf(Cpr.nl(rlat) - f, 1)
        val xz = floor(131072 * mod(lon, dLon) / dLon + 0.5)
        me(23, 17, (yz.toLong() and 0x1FFFF))
        me(40, 17, (xz.toLong() and 0x1FFFF))
    }.finish()

    fun velocity(icao: Int, speedKt: Double, track: Double, verticalFpm: Int): ByteArray = header(icao).apply {
        me(1, 5, 19)
        me(6, 3, 1)
        val vx = speedKt * sin(track * PI / 180)
        val vy = speedKt * cos(track * PI / 180)
        me(14, 1, if (vx < 0) 1 else 0)
        me(15, 10, (abs(vx).roundToInt() + 1).toLong())
        me(25, 1, if (vy < 0) 1 else 0)
        me(26, 10, (abs(vy).roundToInt() + 1).toLong())
        me(37, 1, if (verticalFpm < 0) 1 else 0)
        me(38, 9, (abs(verticalFpm) / 64 + 1).toLong())
    }.finish()
}
