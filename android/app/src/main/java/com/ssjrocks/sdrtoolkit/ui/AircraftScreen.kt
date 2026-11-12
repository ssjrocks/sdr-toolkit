package com.ssjrocks.sdrtoolkit.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Flight
import androidx.compose.material.icons.filled.FlightTakeoff
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.MyLocation
import androidx.compose.material.icons.filled.Radio
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.ssjrocks.sdrtoolkit.SdrApp
import com.ssjrocks.sdrtoolkit.adsb.Aircraft
import com.ssjrocks.sdrtoolkit.adsb.Geo
import com.ssjrocks.sdrtoolkit.engine.ConnState
import com.ssjrocks.sdrtoolkit.engine.RxSettings
import com.ssjrocks.sdrtoolkit.engine.Task
import java.util.Locale
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.pow
import kotlin.math.sin

private enum class Sort(val label: String) { NEAREST("Nearest"), NEWEST("Newest"), HIGHEST("Highest") }

@Composable
fun AircraftScreen(onOpenDevices: () -> Unit, locating: Boolean, onLocate: () -> Unit) {
    val engine = SdrApp.engine
    val conn by engine.conn.collectAsStateWithLifecycle()
    val task by engine.task.collectAsStateWithLifecycle()
    val planes by engine.aircraft.collectAsStateWithLifecycle()
    val stats by engine.adsb.collectAsStateWithLifecycle()
    val s by engine.settings.collectAsStateWithLifecycle()
    var sort by remember { mutableStateOf(Sort.NEAREST) }
    var selected by remember { mutableStateOf<Int?>(null) }
    val me = if (s.myLat != null && s.myLon != null) s.myLat!! to s.myLon!! else null

    if (conn.phase != ConnState.Phase.STREAMING) {
        EmptyState(Icons.Filled.Flight, "Aircraft overhead",
            "Connect a dongle or relay, and see the aircraft around you from their ADS-B transponders on 1090 MHz.",
            "Choose a dongle or relay", onOpenDevices)
        return
    }
    if (task != Task.ADSB) {
        Column(Modifier.fillMaxSize().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center) {
            Icon(Icons.Filled.FlightTakeoff, null, Modifier.size(56.dp), tint = Palette.accentDim)
            Spacer(Modifier.height(16.dp))
            Text("Track aircraft", style = MaterialTheme.typography.titleLarge)
            Spacer(Modifier.height(8.dp))
            Text("The dongle switches to 1090 MHz (2 MS/s) and decodes ADS-B here on the phone. The receiver " +
                "pauses meanwhile. An antenna high up or at a window makes the most difference.",
                style = MaterialTheme.typography.bodyMedium, color = Palette.textDim,
                textAlign = androidx.compose.ui.text.style.TextAlign.Center)
            if (!conn.onLan) {
                Spacer(Modifier.height(8.dp))
                Text("Over the internet this needs about 32 Mbit/s.", style = MaterialTheme.typography.bodySmall,
                    color = Palette.warn)
            }
            Spacer(Modifier.height(20.dp))
            Button(onClick = { engine.setTask(Task.ADSB) }) {
                Icon(Icons.Filled.Flight, null)
                Spacer(Modifier.width(8.dp))
                Text("Start tracking")
            }
        }
        return
    }

    val sorted = when (sort) {
        Sort.NEAREST -> if (me != null) planes.sortedBy { a ->
            if (a.lat != null && a.lon != null) Geo.distanceKm(me.first, me.second, a.lat, a.lon) else Double.MAX_VALUE
        } else planes
        Sort.NEWEST -> planes.sortedByDescending { it.lastSeenMs }
        Sort.HIGHEST -> planes.sortedByDescending { it.altitudeFt ?: Int.MIN_VALUE }
    }

    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("${stats.aircraft} aircraft", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
                    Text(String.format(Locale.US, "%.0f messages a second · %d decoded", stats.messagesPerSec, stats.total),
                        style = MaterialTheme.typography.bodySmall, color = Palette.textDim)
                }
                OutlinedButton(onClick = { engine.setTask(Task.RECEIVER) }) {
                    Icon(Icons.Filled.Radio, null, Modifier.size(18.dp))
                    Spacer(Modifier.width(6.dp))
                    Text("Receiver")
                }
                AircraftMenu(s)
            }
        }
        item {
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically) {
                AssistChip(
                    onClick = onLocate,
                    leadingIcon = {
                        if (locating) CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
                        else Icon(Icons.Filled.MyLocation, null, Modifier.size(18.dp))
                    },
                    label = { Text(if (me == null) "Use my location" else "Location set") },
                )
                for (o in Sort.entries) FilterChip(selected = sort == o, onClick = { sort = o }, label = { Text(o.label) })
            }
        }
        if (planes.any { it.lat != null }) {
            item {
                Card(colors = CardDefaults.cardColors(containerColor = Palette.surface)) {
                    Radar(planes, me, s.miles, selected, Modifier.fillMaxWidth().aspectRatio(1f).padding(8.dp))
                }
            }
        }
        if (planes.isEmpty()) {
            item {
                Hint("Listening on 1090 MHz… Aircraft appear as their transponders are heard. Nothing after a minute? " +
                    "Check the antenna (outside or at a window is best) and that gain isn't too low.")
            }
        }
        items(sorted, key = { it.icao }) { a ->
            AircraftCard(a, me, s.miles, a.icao == selected) { selected = if (selected == a.icao) null else a.icao }
        }
    }
}

@Composable
private fun AircraftMenu(s: RxSettings) {
    val engine = SdrApp.engine
    var open by remember { mutableStateOf(false) }
    Box {
        IconButton(onClick = { open = true }) { Icon(Icons.Filled.MoreVert, "More") }
        DropdownMenu(open, { open = false }) {
            DropdownMenuItem(text = { Text(if (s.miles) "Distances in km" else "Distances in nautical miles") },
                onClick = { open = false; engine.update { it.copy(miles = !it.miles) } })
            for (t in listOf(30, 60, 120, 300)) {
                DropdownMenuItem(text = { Text("Forget aircraft after ${t}s" + if (s.adsbTtlS == t) "  ✓" else "") },
                    onClick = { open = false; engine.update { it.copy(adsbTtlS = t) } })
            }
            DropdownMenuItem(text = { Text("Clear the list") }, onClick = { open = false; engine.clearAircraft() })
        }
    }
}

private fun distanceText(km: Double, miles: Boolean) =
    if (miles) String.format(Locale.US, "%.0f nm", km / 1.852) else String.format(Locale.US, "%.0f km", km)

@Composable
private fun AircraftCard(a: Aircraft, me: Pair<Double, Double>?, miles: Boolean, selected: Boolean, onClick: () -> Unit) {
    val now = System.currentTimeMillis()
    Card(onClick = onClick, colors = CardDefaults.cardColors(
        containerColor = if (selected) Palette.accentDim.copy(alpha = 0.35f) else Palette.surface)) {
        Row(Modifier.padding(horizontal = 16.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(36.dp), contentAlignment = Alignment.Center) {
                Icon(Icons.Filled.Flight, null, tint = if (a.lat != null) Palette.accent else Palette.textDim,
                    modifier = Modifier.size(28.dp).let { m -> a.track?.let { t -> m.rotateBy(t.toFloat()) } ?: m })
            }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.Bottom) {
                    Text(a.callsign.ifEmpty { a.hex }, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                    Spacer(Modifier.width(8.dp))
                    Text(if (a.callsign.isNotEmpty()) a.hex else "", style = MaterialTheme.typography.bodySmall,
                        color = Palette.textDim, fontFamily = Mono)
                    if (a.squawk.isNotEmpty()) {
                        Spacer(Modifier.width(8.dp))
                        val emergency = a.squawk in setOf("7500", "7600", "7700")
                        Text("sq ${a.squawk}", style = MaterialTheme.typography.bodySmall, fontFamily = Mono,
                            color = if (emergency) Palette.bad else Palette.textDim)
                    }
                }
                val parts = listOfNotNull(
                    when {
                        a.onGround -> "on the ground"
                        a.altitudeFt != null -> String.format(Locale.US, "%,d ft", a.altitudeFt) +
                            when {
                                (a.verticalRateFpm ?: 0) > 300 -> " ↑"
                                (a.verticalRateFpm ?: 0) < -300 -> " ↓"
                                else -> ""
                            }
                        else -> null
                    },
                    a.speedKt?.let { String.format(Locale.US, "%.0f kt", it) },
                    a.track?.let { String.format(Locale.US, "%03.0f°", it) },
                )
                Text(parts.joinToString(" · ").ifEmpty { "identifying…" }, style = MaterialTheme.typography.bodyMedium)
            }
            Column(horizontalAlignment = Alignment.End, modifier = Modifier.widthIn(min = 64.dp)) {
                if (me != null && a.lat != null && a.lon != null) {
                    Text(distanceText(Geo.distanceKm(me.first, me.second, a.lat, a.lon), miles),
                        style = MaterialTheme.typography.titleSmall, color = Palette.accent)
                }
                Text(formatAgo(now - a.lastSeenMs), style = MaterialTheme.typography.bodySmall, color = Palette.textDim)
                Text(String.format(Locale.US, "%.0f dB", a.signalDbfs), style = MaterialTheme.typography.bodySmall,
                    color = Palette.textDim)
            }
        }
    }
}

private fun Modifier.rotateBy(deg: Float) = this.rotate(deg)      // the icon points north

/** A plan view, north up: you in the middle (or the aircraft's centre), range rings, each aircraft on its track. */
@Composable
private fun Radar(planes: List<Aircraft>, me: Pair<Double, Double>?, miles: Boolean, selected: Int?, modifier: Modifier) {
    val placed = planes.filter { it.lat != null && it.lon != null }
    val center = me ?: (placed.map { it.lat!! }.average() to placed.map { it.lon!! }.average())
    val farthest = placed.maxOfOrNull { Geo.distanceKm(center.first, center.second, it.lat!!, it.lon!!) } ?: 10.0
    val unit = if (miles) 1.852 else 1.0
    val ringStep = niceRing(farthest / unit / 3) * unit
    val radiusKm = maxOf(ringStep * 3, farthest * 1.08)
    Canvas(modifier) {
        val r = size.minDimension / 2
        val c = Offset(size.width / 2, size.height / 2)
        val paint = android.graphics.Paint().apply {
            color = android.graphics.Color.argb(200, 230, 237, 243)
            textSize = 26f
            isAntiAlias = true
        }
        val dim = android.graphics.Paint(paint).apply { color = android.graphics.Color.argb(140, 139, 148, 158) }
        var k = 1
        while (ringStep * k <= radiusKm) {
            val rr = (ringStep * k / radiusKm * r).toFloat()
            drawCircle(Palette.grid, rr, c, style = Stroke(1.5f))
            // Ring distances down the south-east diagonal, clear of north and the aircraft labels' usual side.
            drawContext.canvas.nativeCanvas.drawText(distanceText(ringStep * k, miles),
                c.x + rr * 0.7071f + 4, c.y + rr * 0.7071f + 22, dim)
            k++
        }
        drawLine(Palette.grid, Offset(c.x, c.y - r), Offset(c.x, c.y + r))
        drawLine(Palette.grid, Offset(c.x - r, c.y), Offset(c.x + r, c.y))
        drawContext.canvas.nativeCanvas.drawText("N", c.x + 8, c.y - r + 28, paint)
        if (me != null) drawCircle(Palette.blue, 7f, c)
        for (a in placed) {
            val d = Geo.distanceKm(center.first, center.second, a.lat!!, a.lon!!)
            val b = Math.toRadians(Geo.bearing(center.first, center.second, a.lat, a.lon))
            val p = Offset(c.x + (d / radiusKm * r * sin(b)).toFloat(), c.y - (d / radiusKm * r * cos(b)).toFloat())
            val stale = System.currentTimeMillis() - a.lastPositionMs > 30_000
            val col = when {
                a.icao == selected -> Palette.warn
                stale -> Palette.textDim
                else -> Palette.accent
            }
            rotate((a.track ?: 0.0).toFloat(), p) {
                val path = Path().apply {
                    moveTo(p.x, p.y - 16f)
                    lineTo(p.x + 10f, p.y + 12f)
                    lineTo(p.x, p.y + 6f)
                    lineTo(p.x - 10f, p.y + 12f)
                    close()
                }
                drawPath(path, col)
            }
            val label = a.callsign.ifEmpty { a.hex } + (a.altitudeFt?.let { "  ${it / 100}" } ?: "")
            drawContext.canvas.nativeCanvas.drawText(label, p.x + 16, p.y + 8, if (stale) dim else paint)
        }
    }
}

private fun niceRing(approx: Double): Double {
    if (approx <= 0) return 1.0
    val p = 10.0.pow(floor(log10(approx)))
    return listOf(1.0, 2.0, 5.0, 10.0).map { it * p }.first { it >= approx }
}
