package com.ssjrocks.sdrtoolkit.ui

import android.graphics.Bitmap
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import com.ssjrocks.sdrtoolkit.dsp.DemodMode
import com.ssjrocks.sdrtoolkit.engine.RxSettings
import com.ssjrocks.sdrtoolkit.engine.SpectrumFrame
import com.ssjrocks.sdrtoolkit.engine.WaterfallBuffer
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.pow
import kotlin.math.roundToLong

/** The waterfall's pixels on the screen side: new rows are copied in as they arrive. */
private class WaterfallImage(val buffer: WaterfallBuffer) {
    val bitmap: Bitmap = Bitmap.createBitmap(buffer.width, buffer.height, Bitmap.Config.ARGB_8888)
    val image = bitmap.asImageBitmap()
    var copied = 0L

    fun sync() {
        val b = buffer
        synchronized(b) {
            val rows = b.rows
            val first = maxOf(copied, rows - b.height)
            for (r in first until rows) {
                val idx = b.indexOf(r)
                bitmap.setPixels(b.pixels, idx * b.width, b.width, 0, idx, b.width, 1)
            }
            copied = rows
        }
        image.prepareToDraw()
    }
}

/**
 * The band: spectrum on top, waterfall below. Tap to tune (to the nearest step), drag to slide
 * along the band (the dongle follows), pinch to zoom, double-tap to see the whole span again.
 */
@Composable
fun SpectrumWaterfall(
    frame: SpectrumFrame?,
    waterfall: WaterfallBuffer,
    settings: RxSettings,
    onTune: (Long) -> Unit,
    onSlide: (Long) -> Unit,
    modifier: Modifier = Modifier,
) {
    val image = remember(waterfall) { WaterfallImage(waterfall) }
    var zoom by remember { mutableFloatStateOf(1f) }
    // Where the view is centred, relative to the dongle's centre.
    var viewOffset by remember { mutableLongStateOf(0L) }
    var slideTo by remember { mutableLongStateOf(-1L) }
    val s by rememberUpdatedState(settings)

    val rate = frame?.rate ?: settings.sampleRate
    val center = frame?.centerHz ?: settings.centerHz
    val span = rate.toDouble() / zoom
    val maxOffset = ((rate - span) / 2).toLong()
    // Keep the tuned frequency in view when it moves outside.
    val tunedRel = settings.tunedHz - settings.centerHz
    if (zoom > 1f && abs(tunedRel - viewOffset) > span / 2) viewOffset = tunedRel.coerceIn(-maxOffset, maxOffset)
    if (viewOffset !in -maxOffset..maxOffset) viewOffset = viewOffset.coerceIn(-maxOffset, maxOffset)
    val viewStart = center + viewOffset - span / 2

    Canvas(
        modifier
            .pointerInput(Unit) {
                detectTapGestures(
                    onTap = { p ->
                        val st = s
                        val r = st.sampleRate.toDouble()
                        val sp = r / zoom
                        val f = st.centerHz + viewOffset - sp / 2 + p.x / size.width * sp
                        val step = st.stepHz.toDouble()
                        onTune(((f / step).roundToLong() * step).toLong())
                    },
                    onDoubleTap = {
                        zoom = 1f
                        viewOffset = 0
                    },
                )
            }
            .pointerInput(Unit) {
                detectTransformGestures { centroid, pan, gestureZoom, _ ->
                    val st = s
                    val r = st.sampleRate.toDouble()
                    val oldSpan = r / zoom
                    val under = centroid.x / size.width - 0.5        // where the fingers are, -0.5..0.5
                    val fAt = viewOffset + under * oldSpan            // the frequency under them
                    val z = (zoom * gestureZoom).coerceIn(1f, 64f)
                    val newSpan = r / z
                    // Keep that frequency under the fingers while zooming; dragging moves the band with them.
                    var off = fAt - under * newSpan - pan.x / size.width * newSpan
                    zoom = z
                    val limit = (r - newSpan) / 2
                    if (abs(off) > limit) {
                        // Past the edge of what the dongle sends: slide the dongle along the band.
                        val excess = if (off > 0) off - limit else off + limit
                        val target = (if (slideTo >= 0) slideTo else st.centerHz) + excess.toLong()
                        slideTo = target
                        onSlide(target)
                        off = off.coerceIn(-limit, limit)
                    } else {
                        slideTo = -1
                    }
                    viewOffset = off.toLong()
                }
            },
    ) {
        val w = size.width
        val h = size.height
        val specH = h * 0.38f
        val floorDb = frame?.floorDb ?: -60f
        val bottomDb = floorDb - 12f
        val topDb = floorDb + maxOf(settings.waterfallRange, 40) + 6f
        fun x(f: Double) = ((f - viewStart) / span * w).toFloat()
        fun y(db: Float) = (specH * (1 - (db - bottomDb) / (topDb - bottomDb))).coerceIn(0f, specH)

        drawRect(Palette.background)
        // ---- grid ----
        val step = niceStep(span / 5)
        var g = ceil(viewStart / step) * step
        val paint = android.graphics.Paint().apply {
            color = android.graphics.Color.argb(170, 230, 237, 243)
            textSize = 26f
            isAntiAlias = true
        }
        while (g < viewStart + span) {
            val gx = x(g)
            drawLine(Palette.grid, Offset(gx, 0f), Offset(gx, specH), 1f)
            drawContext.canvas.nativeCanvas.drawText(formatTick(g, step), gx + 6, specH - 8, paint)
            g += step
        }
        var d = ceil(bottomDb / 10f) * 10f
        while (d < topDb) {
            val gy = y(d)
            drawLine(Palette.grid, Offset(0f, gy), Offset(w, gy), 1f)
            drawContext.canvas.nativeCanvas.drawText("${d.toInt()}", 6f, gy - 4, paint)
            d += 10f
        }

        // ---- the channel being listened to ----
        val tuned = settings.tunedHz.toDouble()
        val bw = settings.bandwidth.toDouble()
        val (lo, hi) = when (settings.mode) {
            DemodMode.USB -> tuned + 100 to tuned + 100 + bw
            DemodMode.LSB -> tuned - 100 - bw to tuned - 100
            else -> tuned - bw / 2 to tuned + bw / 2
        }
        val bandX0 = x(lo)
        val bandX1 = maxOf(x(hi), bandX0 + 2f)
        drawRect(Palette.accent.copy(alpha = 0.16f), Offset(bandX0, 0f), Size(bandX1 - bandX0, h))

        if (frame != null) {
            // ---- spectrum ----
            drawSpectrum(frame.db, frame, viewStart, span, w, ::y, Palette.spectrum, fill = true)
            frame.peak?.let { drawSpectrum(it, frame, viewStart, span, w, ::y, Palette.warn.copy(alpha = 0.6f), fill = false) }

            // ---- waterfall ----
            image.sync()
            val wfTop = specH + 2f
            val wfH = h - wfTop
            val bw0 = waterfall.width
            val srcStart = ((viewStart - (frame.centerHz - frame.rate / 2.0)) / frame.rate * bw0)
            val srcLen = span / frame.rate * bw0
            // Clip to what the dongle actually sends.
            val sx0 = srcStart.coerceIn(0.0, bw0.toDouble())
            val sx1 = (srcStart + srcLen).coerceIn(0.0, bw0.toDouble())
            if (sx1 - sx0 >= 1) {
                val dx0 = ((sx0 - srcStart) / srcLen * w).toFloat()
                val dx1 = ((sx1 - srcStart) / srcLen * w).toFloat()
                val rows = waterfall.rows
                if (rows > 0) {
                    val newest = waterfall.indexOf(rows - 1)
                    val hh = waterfall.height
                    val pieces = listOf(newest to hh - newest, 0 to newest)
                    var dy = wfTop
                    for ((sy, sh) in pieces) {
                        if (sh <= 0) continue
                        val ph = wfH * sh / hh
                        drawImage(
                            image.image,
                            srcOffset = IntOffset(floor(sx0).toInt(), sy),
                            srcSize = IntSize(maxOf(1, ceil(sx1 - sx0).toInt()), sh),
                            dstOffset = IntOffset(dx0.toInt(), dy.toInt()),
                            dstSize = IntSize(maxOf(1, (dx1 - dx0).toInt()), ceil(ph).toInt()),
                            filterQuality = FilterQuality.Low,
                        )
                        dy += ph
                    }
                }
            }
            drawLine(Palette.grid, Offset(0f, specH + 1f), Offset(w, specH + 1f), 2f)
        }

        // ---- the tuned frequency ----
        val tx = x(tuned)
        drawLine(Palette.bad, Offset(tx, 0f), Offset(tx, h), 2f)
    }
}

private fun DrawScope.drawSpectrum(db: FloatArray, frame: SpectrumFrame, viewStart: Double, span: Double, w: Float,
                                   y: (Float) -> Float, color: Color, fill: Boolean) {
    val n = db.size
    val binHz = frame.rate.toDouble() / n
    val first = frame.centerHz - frame.rate / 2.0
    val cols = w.toInt().coerceAtLeast(2)
    val path = Path()
    var started = false
    var lastX = 0f
    var firstX = 0f
    for (c in 0 until cols) {
        val f0 = viewStart + span * c / cols
        val f1 = viewStart + span * (c + 1) / cols
        var k0 = floor((f0 - first) / binHz).toInt()
        var k1 = ceil((f1 - first) / binHz).toInt()
        if (k1 <= 0 || k0 >= n) continue
        k0 = k0.coerceIn(0, n - 1)
        k1 = k1.coerceIn(k0 + 1, n)
        var v = db[k0]
        for (k in k0 + 1 until k1) if (db[k] > v) v = db[k]
        val px = c.toFloat()
        if (!started) {
            path.moveTo(px, y(v))
            firstX = px
            started = true
        } else {
            path.lineTo(px, y(v))
        }
        lastX = px
    }
    if (!started) return
    drawPath(path, color, style = Stroke(1.5f))
    if (fill) {
        val specH = y(Float.NEGATIVE_INFINITY)
        path.lineTo(lastX, specH)
        path.lineTo(firstX, specH)
        path.close()
        drawPath(path, Brush.verticalGradient(listOf(color.copy(alpha = 0.35f), color.copy(alpha = 0.02f)),
            startY = 0f, endY = specH))
    }
}

/** 1, 2 or 5 times a power of ten, at least [approx]. */
private fun niceStep(approx: Double): Double {
    val p = 10.0.pow(floor(log10(approx)))
    return listOf(1.0, 2.0, 5.0, 10.0).map { it * p }.first { it >= approx }
}

private fun formatTick(hz: Double, step: Double): String {
    val decimals = when {
        step >= 1e6 -> 0
        step >= 1e5 -> 1
        step >= 1e4 -> 2
        step >= 1e3 -> 3
        else -> 4
    }
    return String.format(java.util.Locale.US, "%.${decimals}f", hz / 1e6)
}
