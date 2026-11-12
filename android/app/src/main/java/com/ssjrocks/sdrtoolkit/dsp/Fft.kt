package com.ssjrocks.sdrtoolkit.dsp

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.sin

/** In-place radix-2 complex FFT (forward), on separate real and imaginary arrays. */
class Fft(val n: Int) {
    private val cosT = FloatArray(n / 2)
    private val sinT = FloatArray(n / 2)
    private val rev = IntArray(n)

    init {
        require(n >= 2 && n and (n - 1) == 0) { "FFT size must be a power of two" }
        for (k in 0 until n / 2) {
            cosT[k] = cos(2 * PI * k / n).toFloat()
            sinT[k] = sin(2 * PI * k / n).toFloat()
        }
        val bits = Integer.numberOfTrailingZeros(n)
        for (i in 0 until n) rev[i] = Integer.reverse(i) ushr (32 - bits)
    }

    fun transform(re: FloatArray, im: FloatArray) {
        for (i in 0 until n) {
            val j = rev[i]
            if (j > i) {
                var t = re[i]; re[i] = re[j]; re[j] = t
                t = im[i]; im[i] = im[j]; im[j] = t
            }
        }
        var size = 2
        while (size <= n) {
            val half = size / 2
            val step = n / size
            var i = 0
            while (i < n) {
                var k = 0
                for (j in i until i + half) {
                    val wr = cosT[k]
                    val wi = -sinT[k]
                    val h = j + half
                    val tr = wr * re[h] - wi * im[h]
                    val ti = wr * im[h] + wi * re[h]
                    re[h] = re[j] - tr
                    im[h] = im[j] - ti
                    re[j] += tr
                    im[j] += ti
                    k += step
                }
                i += size
            }
            size *= 2
        }
    }
}

/** Averaged power spectrum in dBFS, centre frequency in the middle (a full-scale tone is 0 dBFS). */
class Spectrum(val size: Int) {
    private val fft = Fft(size)
    private val window = FloatArray(size) { blackman(it, size) }
    private val norm = 20 * log10(window.sum().toDouble())
    private val re = FloatArray(size)
    private val im = FloatArray(size)
    private var avg: FloatArray? = null
    private var peak: FloatArray? = null

    /** The newest [size] samples of the ring ([ringI]/[ringQ], next write at [pos]) → dB per bin. */
    fun compute(ringI: FloatArray, ringQ: FloatArray, pos: Int, average: Float, peakHold: Boolean): Frame {
        val n = ringI.size
        for (k in 0 until size) {
            val idx = (pos - size + k + n) % n
            re[k] = ringI[idx] * window[k]
            im[k] = ringQ[idx] * window[k]
        }
        fft.transform(re, im)
        var a = avg
        if (a == null || average <= 0f) {
            a = FloatArray(size)
            avg = a
        }
        val keep = if (avg != null && average > 0f) average else 0f
        val db = FloatArray(size)
        val half = size / 2
        for (k in 0 until size) {
            val p = re[k] * re[k] + im[k] * im[k]
            val s = (k + half) % size                 // fftshift
            // Average the power, not the dB, so peaks don't get pulled down.
            a[s] = if (keep > 0f && a[s] > 0f) (1 - keep) * p + keep * a[s] else p
            db[s] = (10 * log10(a[s] + 1e-20) - norm).toFloat()
        }
        var pk = peak
        if (!peakHold) {
            peak = null
            pk = null
        } else {
            if (pk == null) {
                pk = db.copyOf()
                peak = pk
            } else {
                for (k in 0 until size) pk[k] = maxOf(db[k], pk[k] - 0.05f)    // falls 1.25 dB a second
            }
        }
        return Frame(db, pk?.copyOf())
    }

    fun resetPeak() {
        peak = null
    }

    class Frame(val db: FloatArray, val peak: FloatArray?)

    companion object {
        fun blackman(i: Int, n: Int): Float =
            (0.42 - 0.5 * cos(2 * PI * i / (n - 1)) + 0.08 * cos(4 * PI * i / (n - 1))).toFloat()
    }
}
