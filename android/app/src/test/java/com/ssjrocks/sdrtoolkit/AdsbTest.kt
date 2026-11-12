package com.ssjrocks.sdrtoolkit

import com.ssjrocks.sdrtoolkit.adsb.Cpr
import com.ssjrocks.sdrtoolkit.adsb.Crc
import com.ssjrocks.sdrtoolkit.adsb.ModeSDecoder
import com.ssjrocks.sdrtoolkit.adsb.ModeSDemod
import com.ssjrocks.sdrtoolkit.adsb.ModeSMessage
import com.ssjrocks.sdrtoolkit.adsb.Tracker
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Random
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * ADS-B decoding, checked against the worked examples of "The 1090 MHz Riddle" (Junzi Sun), and
 * the demodulator on IQ synthesised from those messages (pulse-position bits at 2 MS/s).
 */
class AdsbTest {
    private fun hex(s: String) = ByteArray(s.length / 2) { s.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    private val all = { _: Int -> true }

    @Test
    fun crc() {
        assertEquals(0, Crc.residual(hex("8D4840D6202CC371C32CE0576098")))
        val broken = hex("8D4840D6202CC371C32CE0576098")
        broken[6] = (broken[6].toInt() xor 0x10).toByte()
        assertTrue(Crc.residual(broken) != 0)
        assertTrue(Crc.fix112(broken))
        assertEquals("8D4840D6202CC371C32CE0576098", broken.joinToString("") { "%02X".format(it) })
    }

    @Test
    fun identification() {
        val m = ModeSDecoder.decode(hex("8D4840D6202CC371C32CE0576098"), all) as ModeSMessage.Identification
        assertEquals(0x4840D6, m.icao)
        assertEquals("KLM1023", m.callsign)
    }

    @Test
    fun positions() {
        val even = ModeSDecoder.decode(hex("8D40621D58C382D690C8AC2863A7"), all) as ModeSMessage.Position
        val odd = ModeSDecoder.decode(hex("8D40621D58C386435CC412692AD6"), all) as ModeSMessage.Position
        assertFalse(even.odd)
        assertTrue(odd.odd)
        assertEquals(38000, even.altitudeFt)
        val (lat, lon) = Cpr.global(even.latCpr, even.lonCpr, odd.latCpr, odd.lonCpr, oddNewer = false)!!
        assertEquals(52.2572, lat, 1e-4)
        assertEquals(3.91937, lon, 1e-4)
        // Locally, from a reference nearby (the Riddle's example: 52.258, 3.918).
        val (llat, llon) = Cpr.local(even.latCpr, even.lonCpr, false, 52.258, 3.918)
        assertEquals(52.2572, llat, 1e-4)
        assertEquals(3.91937, llon, 1e-4)
    }

    @Test
    fun velocities() {
        val gs = ModeSDecoder.decode(hex("8D485020994409940838175B284F"), all) as ModeSMessage.Velocity
        assertEquals(159.20, gs.speedKt!!, 0.01)
        assertEquals(182.88, gs.track!!, 0.01)
        assertEquals(-832, gs.verticalRateFpm)
        val air = ModeSDecoder.decode(hex("8DA05F219B06B6AF189400CBC33F"), all) as ModeSMessage.Velocity
        assertTrue(air.airspeed)
        assertEquals(375.0, air.speedKt!!, 0.01)
        assertEquals(243.98, air.track!!, 0.01)
        assertEquals(-2304, air.verticalRateFpm)
    }

    @Test
    fun altitudeAndSquawkCodes() {
        val n = (38000 + 1000) / 25
        val code = ((n ushr 5) shl 7) or (((n ushr 4) and 1) shl 5) or 0x10 or (n and 0xF)
        assertEquals(38000, ModeSDecoder.altitude13(code))
        assertEquals("7700", ModeSDecoder.squawk(0b0101010101010))
        // 1200: A1 (bit 2 of 13) and B2 (bit 10) set.
        assertEquals("1200", ModeSDecoder.squawk(0b0100000001000))
    }

    /** Pulse-position IQ at 2 MS/s for [msgs], with gaps, noise and a carrier offset. */
    private fun synth(msgs: List<ByteArray>, amp: Double = 40.0, noise: Double = 3.0): ByteArray {
        val rnd = Random(7)
        val samples = ArrayList<Double>()
        repeat(500) { samples += 0.0 }
        for (m in msgs) {
            val pulses = DoubleArray(16 + m.size * 16)
            for (p in intArrayOf(0, 2, 7, 9)) pulses[p] = 1.0
            for (b in 0 until m.size * 8) {
                val bit = (m[b / 8].toInt() ushr (7 - b % 8)) and 1
                pulses[16 + 2 * b + (1 - bit)] = 1.0
            }
            pulses.forEach { samples += it }
            repeat(700 + rnd.nextInt(300)) { samples += 0.0 }
        }
        val out = ByteArray(samples.size * 2)
        for ((k, a) in samples.withIndex()) {
            val ph = 2 * PI * 37000 * k / 2e6 + 0.3
            out[2 * k] = (127.5 + amp * a * cos(ph) + noise * rnd.nextGaussian()).toInt().coerceIn(0, 255).toByte()
            out[2 * k + 1] = (127.5 + amp * a * sin(ph) + noise * rnd.nextGaussian()).toInt().coerceIn(0, 255).toByte()
        }
        return out
    }

    fun synthPublic(msgs: List<ByteArray>) = synth(msgs)

    @Test
    fun demodulatesAndTracks() {
        var now = 1_000_000L
        val tracker = Tracker(clock = { now })
        val demod = ModeSDemod { m, s -> tracker.feed(m, s) }
        val msgs = listOf("8D4840D6202CC371C32CE0576098", "8D40621D58C386435CC412692AD6",
            "8D40621D58C382D690C8AC2863A7", "8D485020994409940838175B284F").map(::hex)
        val iq = synth(msgs)
        // In awkward chunks, so messages straddle block edges.
        var pos = 0
        var chunk = 777
        while (pos < iq.size) {
            val len = minOf(chunk, iq.size - pos)
            demod.process(iq.copyOfRange(pos, pos + len), len)
            pos += len
            chunk = if (chunk == 777) 4096 else 777
            now += 1
        }
        assertEquals(4L, tracker.good)
        val planes = tracker.snapshot().associateBy { it.hex }
        assertEquals("KLM1023", planes["4840D6"]?.callsign)
        val p = planes["40621D"]!!
        assertEquals(38000, p.altitudeFt)
        assertNotNull(p.lat)
        assertEquals(52.2572, p.lat!!, 1e-4)
        assertEquals(3.91937, p.lon!!, 1e-4)
        assertEquals(159.2, planes["485020"]!!.speedKt!!, 0.01)
        assertTrue(planes["485020"]!!.signalDbfs in -20.0..0.0)

        now += 61_000
        assertTrue(tracker.snapshot().isEmpty())
    }

    @Test
    fun encoderRoundTrip() {
        // Our own encoder (from the standard) against the decoder, around the world.
        for ((lat, lon) in listOf(52.3 to 4.76, -33.94 to 151.18, 40.64 to -73.78, -54.8 to -68.3, 0.01 to 179.99,
            64.13 to -21.94)) {
            val e = ModeSDecoder.decode(AdsbEncoder.position(0xABCDEF, lat, lon, 12000, false), all) as ModeSMessage.Position
            val o = ModeSDecoder.decode(AdsbEncoder.position(0xABCDEF, lat, lon, 12000, true), all) as ModeSMessage.Position
            assertEquals(12000, e.altitudeFt)
            for (oddNewer in listOf(false, true)) {
                val (la, lo) = Cpr.global(e.latCpr, e.lonCpr, o.latCpr, o.lonCpr, oddNewer)!!
                assertEquals("$lat", lat, la, 1e-4)
                assertEquals("$lon", lon, lo, 1e-4)
            }
        }
        val id = ModeSDecoder.decode(AdsbEncoder.identification(0x123456, "QFA1"), all) as ModeSMessage.Identification
        assertEquals("QFA1", id.callsign)
        val v = ModeSDecoder.decode(AdsbEncoder.velocity(0x123456, 300.0, 45.0, -1280), all) as ModeSMessage.Velocity
        assertEquals(300.0, v.speedKt!!, 1.0)
        assertEquals(45.0, v.track!!, 0.3)
        assertEquals(-1280, v.verticalRateFpm)
    }

    @Test
    fun noiseGivesNothing() {
        val tracker = Tracker()
        val demod = ModeSDemod { m, s -> tracker.feed(m, s) }
        val rnd = Random(3)
        val noise = ByteArray(4_000_000) { (127.5 + 8 * rnd.nextGaussian()).toInt().coerceIn(0, 255).toByte() }
        val t0 = System.nanoTime()
        demod.process(noise, noise.size)
        println("Mode S: 1 s of noise took ${(System.nanoTime() - t0) / 1e6} ms, ${demod.preambles} preambles")
        assertTrue(tracker.snapshot().isEmpty())
    }
}
