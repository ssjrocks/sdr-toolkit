package com.ssjrocks.sdrtoolkit

import com.ssjrocks.sdrtoolkit.dsp.AUDIO_RATE
import com.ssjrocks.sdrtoolkit.dsp.DemodMode
import com.ssjrocks.sdrtoolkit.dsp.Demodulator
import com.ssjrocks.sdrtoolkit.dsp.Fft
import com.ssjrocks.sdrtoolkit.dsp.Filters
import com.ssjrocks.sdrtoolkit.dsp.SAMPLE_RATES
import com.ssjrocks.sdrtoolkit.dsp.Spectrum
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Random
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.sin
import kotlin.math.sqrt

/** The receiver's DSP on synthetic signals, as the desktop's tests check receiver/dsp.py. */
class DspTest {
    private fun tone(rate: Int, seconds: Double, phase: (Double) -> Double, amp: (Double) -> Double = { 0.5 },
                     noise: Double = 0.01): Pair<FloatArray, FloatArray> {
        val n = (rate * seconds).toInt()
        val rnd = Random(1)
        val i = FloatArray(n)
        val q = FloatArray(n)
        for (k in 0 until n) {
            val t = k.toDouble() / rate
            val p = phase(t)
            val a = amp(t)
            i[k] = (a * cos(p) + noise * rnd.nextGaussian()).toFloat()
            q[k] = (a * sin(p) + noise * rnd.nextGaussian()).toFloat()
        }
        return i to q
    }

    /** Runs the demodulator over the signal in uneven blocks, as the network delivers it. */
    private fun demod(d: Demodulator, i: FloatArray, q: FloatArray, blocks: IntArray = intArrayOf(32768)): FloatArray {
        val out = ArrayList<Float>()
        var pos = 0
        var b = 0
        val buf = FloatArray(200000)
        while (pos < i.size) {
            val len = minOf(blocks[b++ % blocks.size], i.size - pos)
            val bi = i.copyOfRange(pos, pos + len)
            val bq = q.copyOfRange(pos, pos + len)
            val n = d.process(bi, bq, len, buf)
            for (k in 0 until n) out += buf[k]
            pos += len
        }
        return out.toFloatArray()
    }

    /** Power at [f] Hz (Goertzel), over the second half of the audio (after filters settle). */
    private fun power(audio: FloatArray, f: Double): Double {
        val x = audio.copyOfRange(audio.size / 2, audio.size)
        val w = 2 * PI * f / AUDIO_RATE
        var re = 0.0
        var im = 0.0
        for (k in x.indices) {
            re += x[k] * cos(w * k)
            im -= x[k] * sin(w * k)
        }
        return (re * re + im * im) / (x.size.toDouble() * x.size)
    }

    private fun dominant(audio: FloatArray, candidates: List<Double>) = candidates.maxBy { power(audio, it) }

    @Test
    fun fftFindsTone() {
        val n = 1024
        val fft = Fft(n)
        val re = FloatArray(n) { cos(2 * PI * 100 * it / n).toFloat() }
        val im = FloatArray(n) { sin(2 * PI * 100 * it / n).toFloat() }
        fft.transform(re, im)
        val mags = (0 until n).map { hypot(re[it], im[it]) }
        assertEquals(100, mags.indices.maxBy { mags[it] })
        assertEquals(n.toFloat(), mags[100], 0.01f)
    }

    @Test
    fun spectrumIsInDbfs() {
        val rate = 2400000
        val (i, q) = tone(rate, 0.01, { 2 * PI * 300000 * it }, { 1.0 }, 0.0)
        val s = Spectrum(4096)
        val f = s.compute(i, q, i.size, 0f, false)
        val peak = f.db.indices.maxBy { f.db[it] }
        assertEquals(2048 + 300000.0 / rate * 4096, peak.toDouble(), 1.0)
        assertEquals(0.0, f.db[peak].toDouble(), 1.5)        // a full-scale tone is 0 dBFS
    }

    @Test
    fun stagesAreCheap() {
        for (rate in SAMPLE_RATES) {
            val plan = Filters.planStages(rate, AUDIO_RATE, 6250.0, AUDIO_RATE - 6250.0)
            val product = plan.fold(1) { a, s -> a * s.first }
            assertEquals(rate / AUDIO_RATE, product)
            var r = rate.toDouble()
            var macs = 0.0
            for ((m, taps) in plan) { r /= m; macs += r * taps }
            assertTrue("$rate: $plan costs $macs", macs / rate < 10)      // multiply-adds per input sample
        }
    }

    @Test
    fun nfmTone() {
        val rate = 2400000
        val offset = 150000.0
        val dev = 3000.0
        val (i, q) = tone(rate, 0.5, { t -> 2 * PI * offset * t + dev / 1000.0 * sin(2 * PI * 1000 * t) })
        val d = Demodulator(rate, DemodMode.NFM, offset)
        val audio = demod(d, i, q, intArrayOf(31111, 12000, 65536))
        assertEquals(1000.0, dominant(audio, listOf(300.0, 700.0, 1000.0, 2000.0, 3000.0)), 0.0)
        val rms = sqrt(audio.takeLast(audio.size / 2).map { it * it }.average())
        assertTrue("rms $rms", rms in 0.2..0.8)
        assertEquals(-6.0, d.level, 1.5)          // amplitude 0.5 → -6 dBFS
        assertEquals(AUDIO_RATE / 2.0, audio.size.toDouble(), 50.0)
    }

    @Test
    fun wfmTone() {
        val rate = 2400000
        val offset = -400000.0
        val (i, q) = tone(rate, 0.5, { t -> 2 * PI * offset * t + 45000 / 1000.0 * sin(2 * PI * 1000 * t) })
        val d = Demodulator(rate, DemodMode.WFM, offset)
        val t0 = System.nanoTime()
        val audio = demod(d, i, q)
        val ms = (System.nanoTime() - t0) / 1e6
        println("WFM: 0.5 s at 2.4 MS/s took $ms ms")
        assertEquals(1000.0, dominant(audio, listOf(500.0, 1000.0, 3000.0, 10000.0)), 0.0)
        assertEquals(AUDIO_RATE / 2.0, audio.size.toDouble(), 50.0)
    }

    @Test
    fun sameAudioWhateverTheBlocks() {
        val rate = 960000
        val (i, q) = tone(rate, 0.2, { t -> 2 * PI * 20000 * t + 2.0 * sin(2 * PI * 700 * t) })
        val a = demod(Demodulator(rate, DemodMode.NFM, 20000.0), i, q, intArrayOf(1 shl 20))
        val b = demod(Demodulator(rate, DemodMode.NFM, 20000.0), i, q, intArrayOf(997, 4096, 10001, 33))
        assertEquals(a.size, b.size)
        var worst = 0f
        for (k in a.indices) worst = maxOf(worst, abs(a[k] - b[k]))
        assertTrue("differs by $worst", worst < 1e-3)
    }

    @Test
    fun sidebands() {
        val rate = 1200000
        val carrier = 50000.0
        // A strong tone 1.5 kHz above the carrier and a weaker one 700 Hz below.
        val n = rate / 2
        val i = FloatArray(n)
        val q = FloatArray(n)
        for (k in 0 until n) {
            val t = k.toDouble() / rate
            val a = 2 * PI * (carrier + 1500) * t
            val b = 2 * PI * (carrier - 700) * t
            i[k] = (0.3 * cos(a) + 0.1 * cos(b)).toFloat()
            q[k] = (0.3 * sin(a) + 0.1 * sin(b)).toFloat()
        }
        val usb = demod(Demodulator(rate, DemodMode.USB, carrier), i.copyOf(), q.copyOf())
        assertTrue(power(usb, 1500.0) > 100 * power(usb, 700.0))
        val lsb = demod(Demodulator(rate, DemodMode.LSB, carrier), i.copyOf(), q.copyOf())
        assertTrue(power(lsb, 700.0) > 100 * power(lsb, 1500.0))
        val cw = demod(Demodulator(rate, DemodMode.CW, carrier - 700), i.copyOf(), q.copyOf())
        assertEquals(700.0, dominant(cw, listOf(500.0, 700.0, 1000.0, 1500.0, 2200.0)), 0.0)
    }

    @Test
    fun amTone() {
        val rate = 240000
        val (i, q) = tone(rate, 0.5, { t -> 2 * PI * -30000 * t }, { t -> 0.3 * (1 + 0.5 * sin(2 * PI * 800 * t)) })
        val audio = demod(Demodulator(rate, DemodMode.AM, -30000.0), i, q)
        assertEquals(800.0, dominant(audio, listOf(400.0, 800.0, 1600.0, 3000.0)), 0.0)
    }

    @Test
    fun squelchMutesNoise() {
        val rate = 960000
        val (i, q) = tone(rate, 0.2, { 0.0 }, { 0.0 }, noise = 0.001)
        val d = Demodulator(rate, DemodMode.NFM, 100000.0)
        d.squelch = -40.0
        val audio = demod(d, i, q)
        assertTrue(audio.all { it == 0f })
        assertTrue(!d.isOpen)
    }
}
