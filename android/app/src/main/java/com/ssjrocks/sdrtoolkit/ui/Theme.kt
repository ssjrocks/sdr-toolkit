package com.ssjrocks.sdrtoolkit.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import java.util.Locale
import kotlin.math.abs

object Palette {
    val background = Color(0xFF0E1116)
    val surface = Color(0xFF161B22)
    val surfaceHigh = Color(0xFF1F2630)
    val accent = Color(0xFF4FD1C5)
    val accentDim = Color(0xFF2C7A7B)
    val blue = Color(0xFF63B3ED)
    val good = Color(0xFF48BB78)
    val warn = Color(0xFFECC94B)
    val bad = Color(0xFFF56565)
    val text = Color(0xFFE6EDF3)
    val textDim = Color(0xFF8B949E)
    val spectrum = Color(0xFF7FE7DC)
    val grid = Color(0x33FFFFFF)
}

private val colors = darkColorScheme(
    primary = Palette.accent,
    onPrimary = Color(0xFF00201E),
    primaryContainer = Palette.accentDim,
    onPrimaryContainer = Color(0xFFE6FFFA),
    secondary = Palette.blue,
    onSecondary = Color(0xFF00213A),
    secondaryContainer = Color(0xFF1E3A5F),
    onSecondaryContainer = Color(0xFFD6E9FF),
    background = Palette.background,
    onBackground = Palette.text,
    surface = Palette.background,
    onSurface = Palette.text,
    surfaceVariant = Palette.surfaceHigh,
    onSurfaceVariant = Palette.textDim,
    surfaceContainer = Palette.surface,
    surfaceContainerLow = Palette.surface,
    surfaceContainerHigh = Palette.surfaceHigh,
    surfaceContainerHighest = Color(0xFF28303B),
    outline = Color(0xFF3D4651),
    outlineVariant = Color(0xFF2A313A),
    error = Palette.bad,
)

val Mono = FontFamily.Monospace

@Composable
fun SdrTheme(content: @Composable () -> Unit) {
    val base = Typography()
    MaterialTheme(
        colorScheme = colors,
        typography = base.copy(
            displaySmall = TextStyle(fontFamily = Mono, fontWeight = FontWeight.SemiBold, fontSize = 34.sp,
                letterSpacing = 0.sp),
        ),
        content = content,
    )
}

/** 101100000 → "101.1000" (MHz, four decimals: 100 Hz). */
fun formatMHz(hz: Long): String = String.format(Locale.US, "%.4f", hz / 1e6)

/** 101100000 → "101.100.000", as receivers show it. */
fun formatFreq(hz: Long): String {
    val mhz = hz / 1_000_000
    val khz = (hz / 1000) % 1000
    val h = hz % 1000
    return String.format(Locale.US, "%d.%03d.%03d", mhz, khz, h)
}

/** 12500 → "12.5 kHz", 2400000 → "2.4 MHz". */
fun formatHz(hz: Double): String {
    val a = abs(hz)
    fun trim(v: Double) = String.format(Locale.US, "%.3f", v).trimEnd('0').trimEnd('.')
    return when {
        a >= 1e6 -> "${trim(hz / 1e6)} MHz"
        a >= 1e3 -> "${trim(hz / 1e3)} kHz"
        else -> "${trim(hz)} Hz"
    }
}

fun formatHz(hz: Int) = formatHz(hz.toDouble())

fun formatAgo(ms: Long): String {
    val s = ms / 1000
    return when {
        s < 1 -> "now"
        s < 60 -> "${s}s ago"
        else -> "${s / 60}m ago"
    }
}
