package com.ssjrocks.sdrtoolkit.dsp

import kotlin.math.PI
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.sin

object Filters {
    /** Windowed-sinc (Blackman) low-pass, unity gain at 0 Hz. */
    fun lowpass(taps: Int, cutoff: Double, rate: Double): FloatArray {
        val n = if (taps % 2 == 0) taps + 1 else taps
        val fc = cutoff / rate
        val m = (n - 1) / 2.0
        val h = DoubleArray(n) { i ->
            val x = i - m
            val sinc = if (x == 0.0) 2 * fc else sin(2 * PI * fc * x) / (PI * x)
            sinc * (0.42 - 0.5 * cos(2 * PI * i / (n - 1)) + 0.08 * cos(4 * PI * i / (n - 1)))
        }
        val sum = h.sum()
        return FloatArray(n) { (h[it] / sum).toFloat() }
    }

    /** Taps a Blackman-windowed sinc needs for a transition band this wide (~74 dB stopband). */
    fun tapsFor(rate: Double, transition: Double): Int {
        val t = ceil(5.5 * rate / transition).toInt().coerceAtLeast(5)
        return if (t % 2 == 0) t + 1 else t
    }

    /** Butterworth low-pass as biquads ([b0, b1, b2, a1, a2] each), by the bilinear transform. */
    fun butterworth(order: Int, cutoff: Double, rate: Double): Array<DoubleArray> {
        require(order % 2 == 0)
        val w0 = 2 * PI * (cutoff.coerceAtMost(rate * 0.45)) / rate
        val cw = cos(w0)
        return Array(order / 2) { k ->
            val q = 1 / (2 * sin(PI * (2 * k + 1) / (2 * order)))
            val alpha = sin(w0) / (2 * q)
            val a0 = 1 + alpha
            doubleArrayOf((1 - cw) / 2 / a0, (1 - cw) / a0, (1 - cw) / 2 / a0, -2 * cw / a0, (1 - alpha) / a0)
        }
    }

    /**
     * The cheapest way to decimate by rate / outRate in stages, each a FIR that keeps
     * 0..[pass] Hz and stops what would alias into the final band (from [finalStop] Hz at the end).
     * Returns (factor, taps) per stage.
     */
    fun planStages(rate: Int, outRate: Int, pass: Double, finalStop: Double): List<Pair<Int, Int>> {
        require(rate % outRate == 0) { "$rate isn't a multiple of $outRate" }
        val total = rate / outRate
        if (total == 1) return emptyList()
        var best: List<Pair<Int, Int>>? = null
        var bestCost = Double.MAX_VALUE
        fun cost(stages: List<Int>): Pair<Double, List<Pair<Int, Int>>>? {
            var r = rate.toDouble()
            var c = 0.0
            val out = ArrayList<Pair<Int, Int>>()
            for ((i, m) in stages.withIndex()) {
                val o = r / m
                val stop = if (i == stages.size - 1) finalStop else o - finalStop
                if (stop <= pass * 1.05) return null
                val taps = tapsFor(r, stop - pass)
                c += o * taps
                out += m to taps
                r = o
            }
            return c to out
        }
        fun walk(left: Int, acc: List<Int>) {
            if (left == 1) {
                val r = cost(acc) ?: return
                if (r.first < bestCost) {
                    bestCost = r.first
                    best = r.second
                }
                return
            }
            for (f in 2..left) if (left % f == 0) walk(left / f, acc + f)
        }
        walk(total, emptyList())
        return best ?: listOf(total to tapsFor(rate.toDouble(), (outRate / 2.0 - pass).coerceAtLeast(outRate * 0.05)))
    }
}

/** Complex FIR with decimation: only the kept outputs are computed. History is carried over. */
class ComplexDecimator(private val taps: FloatArray, val factor: Int) {
    private val n = taps.size
    private var wi = FloatArray(0)
    private var wq = FloatArray(0)
    private val hi = FloatArray(n - 1)
    private val hq = FloatArray(n - 1)
    private var start = 0                     // where the next output's window starts, in w

    /** Returns the number of outputs written to [oi]/[oq] (which must hold len / factor + 1). */
    fun process(xi: FloatArray, xq: FloatArray, len: Int, oi: FloatArray, oq: FloatArray): Int {
        val total = n - 1 + len
        if (wi.size < total) {
            wi = FloatArray(total + 1024)
            wq = FloatArray(total + 1024)
        }
        System.arraycopy(hi, 0, wi, 0, n - 1)
        System.arraycopy(hq, 0, wq, 0, n - 1)
        System.arraycopy(xi, 0, wi, n - 1, len)
        System.arraycopy(xq, 0, wq, n - 1, len)
        var p = start
        var k = 0
        val t = taps
        while (p + n <= total) {
            var si = 0f
            var sq = 0f
            for (j in 0 until n) {
                val c = t[j]
                si += c * wi[p + j]
                sq += c * wq[p + j]
            }
            oi[k] = si
            oq[k] = sq
            k++
            p += factor
        }
        start = p - len
        System.arraycopy(wi, len, hi, 0, n - 1)
        System.arraycopy(wq, len, hq, 0, n - 1)
        return k
    }
}

/** Real FIR with decimation. */
class RealDecimator(private val taps: FloatArray, val factor: Int) {
    private val n = taps.size
    private var w = FloatArray(0)
    private val h = FloatArray(n - 1)
    private var start = 0

    fun process(x: FloatArray, len: Int, out: FloatArray): Int {
        val total = n - 1 + len
        if (w.size < total) w = FloatArray(total + 1024)
        System.arraycopy(h, 0, w, 0, n - 1)
        System.arraycopy(x, 0, w, n - 1, len)
        var p = start
        var k = 0
        while (p + n <= total) {
            var s = 0f
            for (j in 0 until n) s += taps[j] * w[p + j]
            out[k++] = s
            p += factor
        }
        start = p - len
        System.arraycopy(w, len, h, 0, n - 1)
        return k
    }
}

/** Biquad cascade (direct form II transposed) on a complex signal: the same filter on I and Q. */
class ComplexIir(private val sos: Array<DoubleArray>) {
    private val zi = Array(sos.size) { DoubleArray(2) }
    private val zq = Array(sos.size) { DoubleArray(2) }

    fun process(xi: FloatArray, xq: FloatArray, len: Int) {
        for ((s, c) in sos.withIndex()) {
            run(xi, len, c, zi[s])
            run(xq, len, c, zq[s])
        }
    }

    private fun run(x: FloatArray, len: Int, c: DoubleArray, z: DoubleArray) {
        val b0 = c[0]; val b1 = c[1]; val b2 = c[2]; val a1 = c[3]; val a2 = c[4]
        var z0 = z[0]
        var z1 = z[1]
        for (i in 0 until len) {
            val v = x[i].toDouble()
            val y = b0 * v + z0
            z0 = b1 * v - a1 * y + z1
            z1 = b2 * v - a2 * y
            x[i] = y.toFloat()
        }
        // Denormals and blow-ups would stall audio for good: start over instead.
        if (!z0.isFinite() || !z1.isFinite()) {
            z0 = 0.0; z1 = 0.0
        }
        z[0] = z0
        z[1] = z1
    }
}

/** Shifts a frequency to 0 Hz, continuing the phase between blocks. */
class Mixer(private val rate: Double, shiftHz: Double = 0.0) {
    var shift = 0.0
        private set
    private var cr = 1.0
    private var ci = 0.0
    private var sr = 1.0
    private var si = 0.0

    init {
        set(shiftHz)
    }

    fun set(shiftHz: Double) {
        shift = shiftHz
        val step = -2 * PI * shiftHz / rate
        sr = cos(step)
        si = sin(step)
    }

    fun process(xi: FloatArray, xq: FloatArray, len: Int) {
        if (shift == 0.0) return
        var r = cr
        var i = ci
        for (k in 0 until len) {
            val a = xi[k]
            val b = xq[k]
            xi[k] = (a * r - b * i).toFloat()
            xq[k] = (a * i + b * r).toFloat()
            val nr = r * sr - i * si
            i = r * si + i * sr
            r = nr
        }
        val mag = kotlin.math.sqrt(r * r + i * i)
        cr = r / mag
        ci = i / mag
    }
}
