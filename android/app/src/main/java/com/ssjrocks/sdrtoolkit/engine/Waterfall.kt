package com.ssjrocks.sdrtoolkit.engine

/**
 * The waterfall's pixels: a ring of rows written by the DSP thread, newest at the lowest index
 * (row index counts down), so the screen can draw it newest-at-top in two pieces.
 */
class WaterfallBuffer(val width: Int, val height: Int = 320) {
    val pixels = IntArray(width * height)

    /** Rows written so far (the newest is at ring index [indexOf] (rows - 1)). */
    @Volatile
    var rows = 0L
        private set

    fun indexOf(row: Long): Int = (height - 1 - (row % height)).toInt()

    /** Adds a row from dB values: [lo] dB is black, [lo] + [range] full colour. */
    fun add(db: FloatArray, lo: Float, range: Float) {
        val idx = indexOf(rows)
        val off = idx * width
        val per = db.size.toFloat() / width
        synchronized(this) {
            for (x in 0 until width) {
                val a = (x * per).toInt()
                val b = maxOf(a + 1, ((x + 1) * per).toInt())
                var v = db[a]
                for (k in a + 1 until minOf(b, db.size)) if (db[k] > v) v = db[k]
                val t = ((v - lo) / range).coerceIn(0f, 1f)
                pixels[off + x] = PALETTE[(t * 255).toInt()]
            }
            rows++
        }
    }

    companion object {
        /** Black → deep blue → cyan → yellow → red → white, like most SDR programs. */
        val PALETTE = IntArray(256) { i ->
            val stops = arrayOf(
                floatArrayOf(0f, 0f, 0f, 0f),
                floatArrayOf(0.15f, 0f, 0f, 80f),
                floatArrayOf(0.35f, 0f, 90f, 200f),
                floatArrayOf(0.55f, 0f, 210f, 210f),
                floatArrayOf(0.72f, 240f, 230f, 0f),
                floatArrayOf(0.88f, 255f, 60f, 0f),
                floatArrayOf(1f, 255f, 255f, 255f),
            )
            val t = i / 255f
            var k = 0
            while (k < stops.size - 2 && t > stops[k + 1][0]) k++
            val s0 = stops[k]
            val s1 = stops[k + 1]
            val f = ((t - s0[0]) / (s1[0] - s0[0])).coerceIn(0f, 1f)
            val r = (s0[1] + (s1[1] - s0[1]) * f).toInt()
            val g = (s0[2] + (s1[2] - s0[2]) * f).toInt()
            val b = (s0[3] + (s1[3] - s0[3]) * f).toInt()
            (0xFF shl 24) or (r shl 16) or (g shl 8) or b
        }
    }
}
