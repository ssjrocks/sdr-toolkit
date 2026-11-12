package com.ssjrocks.sdrtoolkit.dsp

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.exp
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.sqrt

/** Demodulators, as the desktop's receiver offers them (receiver/dsp.py). */
enum class DemodMode(val label: String, val defaultBw: Int, val minBw: Int, val maxBw: Int, val help: String) {
    WFM("WFM", 180000, 50000, 220000, "Broadcast FM"),
    NFM("NFM", 12500, 3000, 25000, "Two-way radio, amateur repeaters, marine"),
    AM("AM", 10000, 2000, 20000, "Airband, broadcast AM"),
    USB("USB", 2700, 500, 5000, "Upper sideband: HF voice above 10 MHz, data"),
    LSB("LSB", 2700, 500, 5000, "Lower sideband: HF voice below 10 MHz"),
    CW("CW", 500, 50, 2000, "Morse, heard as a 700 Hz tone"),
}

const val AUDIO_RATE = 48000

/** Sample rates offered: whole multiples of 48 kHz the RTL2832 can deliver. */
val SAMPLE_RATES = intArrayOf(2880000, 2400000, 1920000, 1440000, 1200000, 960000, 288000, 240000)

/**
 * One channel: shift it to 0 Hz, filter to its bandwidth, demodulate, 48 kHz audio out.
 * Every stage keeps its state between blocks, so there are no clicks at block edges.
 */
class Demodulator(val rate: Int, mode: DemodMode = DemodMode.WFM, offset: Double = 0.0,
                  bandwidth: Int = mode.defaultBw, deemphasisUs: Int = 50) {
    var mode = mode
        private set
    var bandwidth = bandwidth
        private set
    var deemphasisUs = deemphasisUs
        private set

    /** Channel power, dBFS. */
    @Volatile
    var level = -120.0
        private set

    /** Squelch threshold in dBFS, or null for open. */
    @Volatile
    var squelch: Double? = null
    var isOpen = true
        private set

    private val mixer = Mixer(rate.toDouble(), offset)
    private var stages: List<ComplexDecimator> = emptyList()
    var ifRate = AUDIO_RATE
        private set
    private var channel: ComplexIir? = null
    private var audioDecim: RealDecimator? = null
    private var deA = 0.0
    private var deZ = 0.0
    private var ssbIn: Mixer? = null
    private var ssbOut: Mixer? = null
    private var ssbFilter: ComplexIir? = null
    private var lastI = 1f
    private var lastQ = 0f
    private var dc = 0.0
    private var peak = 0.0

    private var bi = FloatArray(0)
    private var bq = FloatArray(0)
    private var ti = FloatArray(0)
    private var tq = FloatArray(0)
    private var disc = FloatArray(0)
    private var audio = FloatArray(0)

    val offset: Double get() = mixer.shift

    init {
        require(rate % AUDIO_RATE == 0) { "sample rate $rate isn't a multiple of $AUDIO_RATE" }
        build()
    }

    fun retune(offsetHz: Double) = mixer.set(offsetHz)

    fun set(mode: DemodMode? = null, bandwidth: Int? = null, deemphasisUs: Int? = null) {
        var changed = false
        if (mode != null && mode != this.mode) {
            this.mode = mode
            if (bandwidth == null) this.bandwidth = mode.defaultBw
            changed = true
        }
        if (bandwidth != null && bandwidth != this.bandwidth) {
            this.bandwidth = bandwidth
            changed = true
        }
        if (deemphasisUs != null && deemphasisUs != this.deemphasisUs) {
            this.deemphasisUs = deemphasisUs
            changed = true
        }
        if (changed) build()
    }

    private fun build() {
        val total = rate / AUDIO_RATE
        val bw = bandwidth.toDouble()
        if (mode == DemodMode.WFM) {
            // The channel at >= 200 kHz (FM is ~180 kHz wide), then down to 48 kHz after demodulation.
            val d1 = if (rate >= 200000) (1..total).filter { total % it == 0 && rate / it >= 200000 }.max() else 1
            ifRate = rate / d1
            val pass = (bw / 2).coerceAtMost(ifRate * 0.45)
            stages = stageFilters(rate, ifRate, pass, (ifRate - pass).coerceAtLeast(pass * 1.2))
            channel = null
            val factor = ifRate / AUDIO_RATE
            audioDecim = RealDecimator(
                Filters.lowpass(Filters.tapsFor(ifRate.toDouble(), AUDIO_RATE - 2 * 15000.0), 16000.0, ifRate.toDouble()),
                factor)
            val tau = deemphasisUs * 1e-6
            deA = if (tau > 0) exp(-1 / (ifRate * tau)) else 0.0
            ssbIn = null; ssbOut = null; ssbFilter = null
        } else {
            ifRate = AUDIO_RATE
            val sideband = mode == DemodMode.USB || mode == DemodMode.LSB || mode == DemodMode.CW
            val pass = if (sideband) max(bw, 600.0) + 300 else max(bw / 2, 500.0)
            stages = stageFilters(rate, AUDIO_RATE, pass, AUDIO_RATE - pass)
            channel = if (sideband) null else ComplexIir(Filters.butterworth(6, pass, AUDIO_RATE.toDouble()))
            audioDecim = null
            if (sideband) {
                // Sideband selection at 48 kHz: move the passband's middle to 0 Hz, filter to half the
                // bandwidth, move it back; the real part is the audio. USB's passband is 100 Hz to
                // 100 Hz + bandwidth above the carrier, LSB's the same below. CW: the signal at 0 Hz,
                // filtered narrow, then moved up to the 700 Hz tone.
                val half = bw / 2
                val (into, back) = when (mode) {
                    DemodMode.USB -> (half + 100) to (half + 100)
                    DemodMode.LSB -> -(half + 100) to -(half + 100)
                    else -> 0.0 to -CW_TONE
                }
                ssbIn = Mixer(AUDIO_RATE.toDouble(), into)
                ssbOut = Mixer(AUDIO_RATE.toDouble(), -back)
                ssbFilter = ComplexIir(Filters.butterworth(6, half, AUDIO_RATE.toDouble()))
            } else {
                ssbIn = null; ssbOut = null; ssbFilter = null
            }
        }
        lastI = 1f; lastQ = 0f
        dc = 0.0
        deZ = 0.0
    }

    private fun stageFilters(rate: Int, out: Int, pass: Double, finalStop: Double): List<ComplexDecimator> {
        var r = rate.toDouble()
        val plan = Filters.planStages(rate, out, pass, finalStop)
        return plan.mapIndexed { i, (m, taps) ->
            val o = r / m
            val stop = if (i == plan.size - 1) finalStop else o - finalStop
            val f = ComplexDecimator(Filters.lowpass(taps, (pass + stop) / 2, r), m)
            r = o
            f
        }
    }

    private fun ensure(n: Int) {
        if (bi.size < n) {
            bi = FloatArray(n); bq = FloatArray(n)
            ti = FloatArray(n); tq = FloatArray(n)
            disc = FloatArray(n); audio = FloatArray(n)
        }
    }

    /**
     * Complex samples at the input rate (modified in place) → audio at 48 kHz, -1..1.
     * Returns the number of audio samples in [out] (which must hold len * 48000 / rate + 8).
     */
    fun process(xi: FloatArray, xq: FloatArray, len: Int, out: FloatArray): Int {
        ensure(len + 16)
        mixer.process(xi, xq, len)
        var ci = xi
        var cq = xq
        var n = len
        var useT = false
        for (s in stages) {
            val oi = if (useT) bi else ti
            val oq = if (useT) bq else tq
            n = s.process(ci, cq, n, oi, oq)
            ci = oi; cq = oq
            useT = !useT
        }
        if (ci === xi) {                         // no decimation at all: work on a copy
            System.arraycopy(xi, 0, ti, 0, n); System.arraycopy(xq, 0, tq, 0, n)
            ci = ti; cq = tq
        }
        channel?.process(ci, cq, n)
        if (n > 0) {
            var p = 0.0
            for (k in 0 until n) p += ci[k] * ci[k] + cq[k] * cq[k]
            level = 10 * log10(p / n + 1e-20)
        }
        isOpen = squelch.let { it == null || level >= it }
        val count: Int
        when (mode) {
            DemodMode.WFM, DemodMode.NFM -> {
                var li = lastI
                var lq = lastQ
                for (k in 0 until n) {
                    val a = ci[k]
                    val b = cq[k]
                    // angle of y[k] * conj(y[k-1])
                    disc[k] = atan2(b * li - a * lq, a * li + b * lq)
                    li = a; lq = b
                }
                if (n > 0) { lastI = li; lastQ = lq }
                if (mode == DemodMode.WFM) {
                    // 75 kHz deviation -> +-1, then de-emphasis (one pole), then 15 kHz low-pass and 48 kHz.
                    val scale = (ifRate / (2 * PI * 75000)).toFloat()
                    var z = deZ
                    val a = deA
                    for (k in 0 until n) {
                        val v = disc[k] * scale
                        z = if (a > 0) (1 - a) * v + a * z else v.toDouble()
                        disc[k] = z.toFloat()
                    }
                    deZ = if (z.isFinite()) z else 0.0
                    count = audioDecim!!.process(disc, n, out)
                } else {
                    val scale = (AUDIO_RATE / (2 * PI * max(bandwidth / 2 * 0.8, 1500.0))).toFloat()
                    for (k in 0 until n) out[k] = disc[k] * scale
                    count = n
                }
            }
            DemodMode.AM -> {
                var mean = 0.0
                for (k in 0 until n) {
                    val e = sqrt(ci[k] * ci[k] + cq[k] * cq[k])
                    disc[k] = e
                    mean += e
                }
                if (n > 0) {   // the carrier level, followed slowly, is the DC to remove
                    mean /= n
                    dc = if (dc == 0.0) mean else 0.9 * dc + 0.1 * mean
                }
                for (k in 0 until n) disc[k] = (disc[k] - dc).toFloat()
                agc(disc, n)
                System.arraycopy(disc, 0, out, 0, n)
                count = n
            }
            else -> {
                ssbIn!!.process(ci, cq, n)
                ssbFilter!!.process(ci, cq, n)
                ssbOut!!.process(ci, cq, n)
                System.arraycopy(ci, 0, disc, 0, n)
                agc(disc, n)
                System.arraycopy(disc, 0, out, 0, n)
                count = n
            }
        }
        if (!isOpen) java.util.Arrays.fill(out, 0, count, 0f)
        return count
    }

    /**
     * AGC for AM and SSB: follows the signal's peak, rising at once and falling slowly (half in
     * about a second), so pauses between words don't pump the noise up. The gain is capped, so
     * pure noise isn't raised to full volume either.
     */
    private fun agc(x: FloatArray, n: Int) {
        if (n == 0) return
        val mags = FloatArray(n) { abs(x[it]) }
        mags.sort()
        val p = mags[((n - 1) * 0.99).toInt()] + 1e-12
        val release = 0.5.pow(n.toDouble() / AUDIO_RATE / AGC_RELEASE)
        peak = if (p > peak) p else max(p, peak * release)
        val g = (0.5 / max(peak, 0.5 / AGC_MAX_GAIN)).toFloat()
        for (k in 0 until n) x[k] *= g
    }

    companion object {
        const val CW_TONE = 700.0
        const val AGC_RELEASE = 1.0
        const val AGC_MAX_GAIN = 2000.0
    }
}
