package com.ssjrocks.sdrtoolkit.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.VolumeOff
import androidx.compose.material.icons.automirrored.filled.VolumeUp
import androidx.compose.material.icons.filled.ChevronLeft
import androidx.compose.material.icons.filled.ChevronRight
import androidx.compose.material.icons.filled.Flight
import androidx.compose.material.icons.filled.Radio
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.ssjrocks.sdrtoolkit.SdrApp
import com.ssjrocks.sdrtoolkit.dsp.DemodMode
import com.ssjrocks.sdrtoolkit.dsp.SAMPLE_RATES
import com.ssjrocks.sdrtoolkit.engine.ConnState
import com.ssjrocks.sdrtoolkit.engine.RxSettings
import com.ssjrocks.sdrtoolkit.engine.Task
import java.util.Locale
import kotlin.math.roundToInt

private val STEPS = intArrayOf(100, 1000, 5000, 6250, 10000, 12500, 25000, 100000, 200000, 1000000)

/** Places to start listening (as the desktop's "How to tune" lists them). */
private data class Band(val name: String, val note: String, val hz: Long, val mode: DemodMode, val step: Int)

private val BANDS = listOf(
    Band("FM broadcast", "88–108 MHz", 98_000_000, DemodMode.WFM, 100_000),
    Band("Airband", "118–137 MHz, AM voice", 127_000_000, DemodMode.AM, 25_000),
    Band("2 m amateur", "144–148 MHz, repeaters and simplex", 145_500_000, DemodMode.NFM, 12_500),
    Band("Marine VHF", "Channel 16, 156.800 MHz", 156_800_000, DemodMode.NFM, 25_000),
    Band("NOAA weather radio", "162.400–162.550 MHz (US)", 162_550_000, DemodMode.NFM, 25_000),
    Band("ISM 433 MHz", "Sensors, key fobs", 433_920_000, DemodMode.NFM, 25_000),
    Band("70 cm amateur", "430–450 MHz", 439_000_000, DemodMode.NFM, 12_500),
    Band("PMR446", "446.0–446.2 MHz, licence-free radios", 446_006_250, DemodMode.NFM, 12_500),
    Band("UHF CB (Australia)", "476–477 MHz, channel 11 is 476.525", 476_525_000, DemodMode.NFM, 25_000),
    Band("40 m amateur", "7.1 MHz LSB: needs direct sampling or a V4", 7_100_000, DemodMode.LSB, 1_000),
    Band("20 m amateur", "14.2 MHz USB: needs direct sampling or a V4", 14_200_000, DemodMode.USB, 1_000),
    Band("Shortwave broadcast", "9.4–9.9 MHz AM: needs direct sampling or a V4", 9_600_000, DemodMode.AM, 5_000),
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ReceiverScreen(onOpenDevices: () -> Unit, onOpenAircraft: () -> Unit) {
    val engine = SdrApp.engine
    val s by engine.settings.collectAsStateWithLifecycle()
    val conn by engine.conn.collectAsStateWithLifecycle()
    val task by engine.task.collectAsStateWithLifecycle()
    val frame by engine.spectrum.collectAsStateWithLifecycle()
    val stats by engine.stats.collectAsStateWithLifecycle()
    var editing by remember { mutableStateOf(false) }
    var showBands by remember { mutableStateOf(false) }
    var showSettings by remember { mutableStateOf(false) }
    val streaming = conn.phase == ConnState.Phase.STREAMING

    Column(Modifier.fillMaxSize()) {
        // ---- frequency ----
        Surface(color = Palette.surface) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 8.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    StepButton(Icons.Filled.ChevronLeft, "Down a step") { engine.tune(s.tunedHz - s.stepHz) }
                    Column(Modifier.weight(1f).clickable { editing = true }, horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(formatFreq(s.tunedHz), style = MaterialTheme.typography.displaySmall, color = Palette.text,
                            maxLines = 1)
                        Text("MHz · ${s.mode.label} · ${formatHz(s.bandwidth)} wide", style = MaterialTheme.typography.bodySmall,
                            color = Palette.textDim)
                    }
                    StepButton(Icons.Filled.ChevronRight, "Up a step") { engine.tune(s.tunedHz + s.stepHz) }
                }
                Spacer(Modifier.height(6.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    StepMenu(s.stepHz) { st -> engine.update { it.copy(stepHz = st) } }
                    Spacer(Modifier.width(8.dp))
                    SignalMeter(stats.levelDb, if (s.squelchOn) s.squelchDb.toFloat() else null, stats.squelchOpen,
                        Modifier.weight(1f))
                }
            }
        }

        // ---- the band ----
        Box(Modifier.weight(1f).fillMaxWidth()) {
            when {
                !streaming -> EmptyState(Icons.Filled.Radio, "No radio connected",
                    "Plug in an RTL-SDR or pick a relay, and the band shows up here.",
                    "Choose a dongle or relay", onOpenDevices)
                task == Task.ADSB -> EmptyState(Icons.Filled.Flight, "Tracking aircraft",
                    "The dongle is on 1090 MHz for ADS-B. The receiver carries on where it was when you switch back.",
                    "Back to the receiver") { engine.setTask(Task.RECEIVER) }
                else -> SpectrumWaterfall(
                    frame = frame,
                    waterfall = engine.waterfall,
                    settings = s,
                    onTune = engine::tune,
                    onSlide = engine::setCenter,
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }

        // ---- mode and audio ----
        Surface(color = Palette.surface) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp)) {
                SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                    DemodMode.entries.forEachIndexed { i, m ->
                        SegmentedButton(
                            selected = s.mode == m,
                            onClick = { engine.setMode(m) },
                            shape = SegmentedButtonDefaults.itemShape(i, DemodMode.entries.size),
                            icon = {},
                            label = { Text(m.label, maxLines = 1) },
                        )
                    }
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    IconButton(onClick = { engine.update { it.copy(muted = !it.muted) } }) {
                        Icon(if (s.muted) Icons.AutoMirrored.Filled.VolumeOff else Icons.AutoMirrored.Filled.VolumeUp,
                            contentDescription = if (s.muted) "Unmute" else "Mute",
                            tint = if (s.muted) Palette.bad else Palette.text)
                    }
                    Slider(s.volume, { v -> engine.update { it.copy(volume = v, muted = false) } },
                        valueRange = 0f..2f, modifier = Modifier.weight(1f))
                    IconButton(onClick = { showBands = true }) { Icon(Icons.AutoMirrored.Filled.List, "Bands") }
                    IconButton(onClick = { showSettings = true }) { Icon(Icons.Filled.Tune, "Receiver settings") }
                }
            }
        }
    }

    if (editing) FrequencyDialog(s.tunedHz, onDismiss = { editing = false }) { hz ->
        editing = false
        engine.tune(hz)
    }
    if (showBands) {
        ModalBottomSheet(onDismissRequest = { showBands = false }, sheetState = rememberModalBottomSheetState(true),
            containerColor = Palette.surface) {
            Column(Modifier.verticalScroll(rememberScrollState()).padding(bottom = 24.dp)) {
                Text("Bands", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(horizontal = 24.dp))
                ListItem(
                    headlineContent = { Text("ADS-B aircraft") },
                    supportingContent = { Text("1090 MHz: positions, callsigns and altitudes") },
                    leadingContent = { Icon(Icons.Filled.Flight, null, tint = Palette.accent) },
                    colors = ListItemDefaults.colors(containerColor = Palette.surface),
                    modifier = Modifier.clickable { showBands = false; onOpenAircraft() },
                )
                for (b in BANDS) {
                    ListItem(
                        headlineContent = { Text(b.name) },
                        supportingContent = { Text(b.note) },
                        trailingContent = { Text(b.mode.label, color = Palette.accent) },
                        colors = ListItemDefaults.colors(containerColor = Palette.surface),
                        modifier = Modifier.clickable {
                            showBands = false
                            engine.update { it.copy(mode = b.mode, bandwidth = b.mode.defaultBw, stepHz = b.step) }
                            engine.tune(b.hz)
                        },
                    )
                }
            }
        }
    }
    if (showSettings) {
        ModalBottomSheet(onDismissRequest = { showSettings = false }, sheetState = rememberModalBottomSheetState(true),
            containerColor = Palette.surface) {
            ReceiverSettings(s, conn) { change -> engine.update(change) }
        }
    }
}

@Composable
private fun StepButton(icon: androidx.compose.ui.graphics.vector.ImageVector, label: String, onClick: () -> Unit) {
    FilledIconButton(onClick = onClick, modifier = Modifier.size(52.dp),
        colors = IconButtonDefaults.filledIconButtonColors(containerColor = Palette.surfaceHigh, contentColor = Palette.text)) {
        Icon(icon, label, Modifier.size(32.dp))
    }
}

@Composable
private fun StepMenu(step: Int, onPick: (Int) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        TextButton(onClick = { open = true }) { Text("Step ${formatHz(step)}") }
        DropdownMenu(open, { open = false }) {
            for (st in STEPS) DropdownMenuItem(text = { Text(formatHz(st)) }, onClick = { open = false; onPick(st) })
        }
    }
}

/** The channel's level, -100 to 0 dBFS, with the squelch threshold marked. */
@Composable
private fun SignalMeter(levelDb: Float, squelch: Float?, open: Boolean, modifier: Modifier) {
    Row(modifier, verticalAlignment = Alignment.CenterVertically) {
        val frac = ((levelDb + 100f) / 100f).coerceIn(0f, 1f)
        Box(Modifier.weight(1f).height(10.dp).clip(RoundedCornerShape(5.dp)).background(Palette.surfaceHigh)) {
            Box(Modifier.fillMaxWidth(frac).fillMaxHeight().background(
                if (open) Palette.accent else Palette.textDim))
            if (squelch != null) {
                val sq = ((squelch + 100f) / 100f).coerceIn(0f, 1f)
                Box(Modifier.fillMaxWidth(sq).fillMaxHeight()) {
                    Box(Modifier.align(Alignment.CenterEnd).width(2.dp).fillMaxHeight().background(Palette.warn))
                }
            }
        }
        Spacer(Modifier.width(8.dp))
        Text(String.format(Locale.US, "%4.0f dB", levelDb), style = MaterialTheme.typography.labelMedium,
            fontFamily = Mono, color = Palette.textDim)
    }
}

@Composable
fun EmptyState(icon: androidx.compose.ui.graphics.vector.ImageVector, title: String, text: String,
               action: String, onAction: () -> Unit) {
    Column(Modifier.fillMaxSize().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center) {
        Icon(icon, null, Modifier.size(56.dp), tint = Palette.accentDim)
        Spacer(Modifier.height(16.dp))
        Text(title, style = MaterialTheme.typography.titleLarge, textAlign = TextAlign.Center)
        Spacer(Modifier.height(8.dp))
        Text(text, style = MaterialTheme.typography.bodyMedium, color = Palette.textDim, textAlign = TextAlign.Center)
        Spacer(Modifier.height(20.dp))
        Button(onClick = onAction) { Text(action) }
    }
}

@Composable
private fun FrequencyDialog(current: Long, onDismiss: () -> Unit, onTune: (Long) -> Unit) {
    var text by remember { mutableStateOf(String.format(Locale.US, "%.6f", current / 1e6).trimEnd('0').trimEnd('.')) }
    val hz = parseFrequency(text)
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Frequency") },
        text = {
            OutlinedTextField(text, { text = it }, singleLine = true, label = { Text("MHz") },
                supportingText = { Text("e.g. 101.1, 145.5, or 7100k") },
                isError = hz == null,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal, imeAction = ImeAction.Go),
                keyboardActions = KeyboardActions(onGo = { hz?.let(onTune) }))
        },
        confirmButton = { TextButton(onClick = { hz?.let(onTune) }, enabled = hz != null) { Text("Tune") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

/** "101.1" (MHz), "7100k", "1090M", "433920000" (Hz, when it's too big to be MHz). */
fun parseFrequency(text: String): Long? {
    val t = text.trim().lowercase(Locale.US).removeSuffix("hz").trim().replace(",", ".")
    val (num, mult) = when {
        t.endsWith("k") -> t.dropLast(1) to 1e3
        t.endsWith("m") -> t.dropLast(1) to 1e6
        t.endsWith("g") -> t.dropLast(1) to 1e9
        else -> t to 0.0
    }
    val v = num.trim().toDoubleOrNull() ?: return null
    val hz = when {
        mult > 0 -> v * mult
        v >= 100_000 -> v                  // plain hertz
        else -> v * 1e6
    }
    return if (hz in 100_000.0..2_200_000_000.0) hz.toLong() else null
}

@Composable
private fun ReceiverSettings(s: RxSettings, conn: ConnState, update: ((RxSettings) -> RxSettings) -> Unit) {
    Column(Modifier.verticalScroll(rememberScrollState()).padding(horizontal = 24.dp).padding(bottom = 32.dp)) {
        Text("Receiver", style = MaterialTheme.typography.titleLarge)

        Heading("Signal")
        val m = s.mode
        LabeledSlider("Bandwidth", formatHz(s.bandwidth), s.bandwidth.toFloat(), m.minBw.toFloat()..m.maxBw.toFloat()) { v ->
            val stepped = (v / 100).roundToInt() * 100
            update { it.copy(bandwidth = stepped.coerceIn(m.minBw, m.maxBw)) }
        }
        SwitchRow("Squelch", "Mute while the channel is weaker than the level below", s.squelchOn) { on ->
            update { it.copy(squelchOn = on) }
        }
        if (s.squelchOn) {
            LabeledSlider("Squelch level", "${s.squelchDb} dBFS", s.squelchDb.toFloat(), -120f..0f) { v ->
                update { it.copy(squelchDb = v.roundToInt()) }
            }
        }
        if (m == DemodMode.WFM) {
            Choice("De-emphasis", listOf(50 to "50 µs", 75 to "75 µs (Americas)", 0 to "Off"), s.deemphasisUs) { v ->
                update { it.copy(deemphasisUs = v) }
            }
        }

        Heading("Dongle")
        DropdownChoice("Sample rate",
            SAMPLE_RATES.map { it to String.format(Locale.US, "%.3g MS/s · %s of band", it / 1e6, formatHz(it * 0.9)) },
            s.sampleRate) { v -> update { it.copy(sampleRate = v) } }
        Text("How wide a slice of the band you see. Over the internet, lower is gentler on the connection.",
            style = MaterialTheme.typography.bodySmall, color = Palette.textDim)
        SwitchRow("Automatic gain", "Let the tuner set its own gain", s.gainAuto) { on -> update { it.copy(gainAuto = on) } }
        if (!s.gainAuto) {
            val gains = conn.gains
            val idx = gains.indices.minByOrNull { kotlin.math.abs(gains[it] - s.gainTenths) } ?: 0
            LabeledSlider("Gain", String.format(Locale.US, "%.1f dB", gains[idx] / 10.0), idx.toFloat(),
                0f..(gains.size - 1).coerceAtLeast(1).toFloat(), steps = (gains.size - 2).coerceAtLeast(0)) { v ->
                update { it.copy(gainTenths = gains[v.roundToInt().coerceIn(0, gains.size - 1)]) }
            }
            Text("Lower it if strong stations make ghost signals; raise it for weak ones.",
                style = MaterialTheme.typography.bodySmall, color = Palette.textDim)
        }
        LabeledSlider("Frequency correction", "${s.ppm} ppm", s.ppm.toFloat(), -100f..100f, steps = 199) { v ->
            update { it.copy(ppm = v.roundToInt()) }
        }
        if (conn.isRelay) {
            Text("A relay applies its own correction (${conn.relayPpm} ppm), set on its PC.",
                style = MaterialTheme.typography.bodySmall, color = Palette.textDim)
        }
        Choice("Direct sampling (HF)", listOf(0 to "Off", 1 to "I branch", 2 to "Q branch (V3)"), s.directSampling) { v ->
            update { it.copy(directSampling = v) }
        }
        Text("Below 24 MHz on a dongle without an upconverter. The RTL-SDR Blog V4 doesn't need it.",
            style = MaterialTheme.typography.bodySmall, color = Palette.textDim)
        SwitchRow("Bias tee", "4.5 V on the antenna socket for an active antenna or LNA. Never with a DC-shorted antenna.",
            s.biasTee) { on -> update { it.copy(biasTee = on) } }
        SwitchRow("RTL AGC", "The RTL2832's digital gain control; usually off", s.rtlAgc) { on -> update { it.copy(rtlAgc = on) } }
        if (conn.tunerType == 1) {
            SwitchRow("Offset tuning", "E4000 tuners: moves the DC spike away", s.offsetTuning) { on ->
                update { it.copy(offsetTuning = on) }
            }
        }

        Heading("Display")
        Choice("FFT size", listOf(1024 to "1k", 2048 to "2k", 4096 to "4k", 8192 to "8k"), s.fftSize) { v ->
            update { it.copy(fftSize = v) }
        }
        LabeledSlider("Averaging", String.format(Locale.US, "%.0f%%", s.averaging * 100), s.averaging, 0f..0.95f) { v ->
            update { it.copy(averaging = v) }
        }
        LabeledSlider("Waterfall range", "${s.waterfallRange} dB", s.waterfallRange.toFloat(), 20f..100f) { v ->
            update { it.copy(waterfallRange = v.roundToInt()) }
        }
        SwitchRow("Peak hold", "Show the highest level each frequency has reached", s.peakHold) { on ->
            update { it.copy(peakHold = on) }
        }
    }
}

@Composable
private fun Heading(text: String) {
    Spacer(Modifier.height(20.dp))
    Text(text.uppercase(Locale.US), style = MaterialTheme.typography.labelMedium, color = Palette.accent)
    HorizontalDivider(Modifier.padding(top = 4.dp, bottom = 8.dp), color = Palette.surfaceHigh)
}

@Composable
fun SwitchRow(title: String, subtitle: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().clickable { onChange(!checked) }.padding(vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, style = MaterialTheme.typography.bodySmall, color = Palette.textDim)
        }
        Spacer(Modifier.width(12.dp))
        Switch(checked, onChange)
    }
}

@Composable
private fun LabeledSlider(title: String, value: String, v: Float, range: ClosedFloatingPointRange<Float>, steps: Int = 0,
                          onChange: (Float) -> Unit) {
    Column(Modifier.padding(vertical = 4.dp)) {
        Row {
            Text(title, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
            Text(value, style = MaterialTheme.typography.bodyMedium, color = Palette.accent, fontFamily = Mono)
        }
        Slider(v.coerceIn(range.start, range.endInclusive), onChange, valueRange = range, steps = steps)
    }
}

@Composable
private fun <T> Choice(title: String, options: List<Pair<T, String>>, selected: T, onPick: (T) -> Unit) {
    Column(Modifier.padding(vertical = 6.dp)) {
        Text(title, style = MaterialTheme.typography.bodyLarge)
        Spacer(Modifier.height(6.dp))
        SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
            options.forEachIndexed { i, (value, label) ->
                SegmentedButton(selected = value == selected, onClick = { onPick(value) },
                    shape = SegmentedButtonDefaults.itemShape(i, options.size), icon = {},
                    label = { Text(label, maxLines = 1, fontWeight = if (value == selected) FontWeight.SemiBold else null) })
            }
        }
    }
}

@Composable
private fun <T> DropdownChoice(title: String, options: List<Pair<T, String>>, selected: T, onPick: (T) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Row(Modifier.fillMaxWidth().padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
        Box {
            TextButton(onClick = { open = true }) {
                Text(options.firstOrNull { it.first == selected }?.second ?: "$selected")
            }
            DropdownMenu(open, { open = false }) {
                for ((v, label) in options) DropdownMenuItem(text = { Text(label) }, onClick = { open = false; onPick(v) })
            }
        }
    }
}
