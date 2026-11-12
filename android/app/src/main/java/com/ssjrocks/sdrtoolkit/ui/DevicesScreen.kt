package com.ssjrocks.sdrtoolkit.ui

import android.content.pm.PackageManager
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material.icons.filled.Lan
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Public
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Router
import androidx.compose.material.icons.filled.Usb
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.ssjrocks.sdrtoolkit.SdrApp
import com.ssjrocks.sdrtoolkit.core.Relay
import com.ssjrocks.sdrtoolkit.core.RelayInfo
import com.ssjrocks.sdrtoolkit.engine.ConnState
import com.ssjrocks.sdrtoolkit.engine.RtlDriver
import com.ssjrocks.sdrtoolkit.engine.SavedSource
import com.ssjrocks.sdrtoolkit.engine.Source
import com.ssjrocks.sdrtoolkit.engine.UsbDongle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** What a password is wanted for: a relay from a list, or an address. */
private data class PasswordAsk(val title: String, val key: String, val retry: Boolean, val connect: (String) -> Unit)

@Composable
fun DevicesScreen(onUseUsb: (UsbDongle) -> Unit, driverError: String?, onDriverErrorSeen: () -> Unit) {
    val engine = SdrApp.engine
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val conn by engine.conn.collectAsStateWithLifecycle()
    val dongles by SdrApp.usb.list.collectAsStateWithLifecycle()
    val saved by engine.sources.collectAsStateWithLifecycle()

    var lan by remember { mutableStateOf<List<RelayInfo>>(emptyList()) }
    var scanning by remember { mutableStateOf(false) }
    var scanned by remember { mutableStateOf(false) }
    var internet by remember { mutableStateOf<List<RelayInfo>?>(null) }
    var fetching by remember { mutableStateOf(false) }
    var fetchError by remember { mutableStateOf<String?>(null) }
    var ask by remember { mutableStateOf<PasswordAsk?>(null) }
    var adding by remember { mutableStateOf(false) }
    var needDriver by remember { mutableStateOf(false) }
    val hasUsbHost = remember { context.packageManager.hasSystemFeature(PackageManager.FEATURE_USB_HOST) }

    fun scan() {
        if (scanning) return
        scanning = true
        scope.launch {
            lan = withContext(Dispatchers.IO) { runCatching { Relay.discover(1500) }.getOrDefault(emptyList()) }
            scanning = false
            scanned = true
        }
    }

    fun fetch() {
        fetching = true
        fetchError = null
        scope.launch {
            val r = withContext(Dispatchers.IO) { runCatching { Relay.directory() } }
            internet = r.getOrNull()
            fetchError = r.exceptionOrNull()?.let { "Couldn't get the list: ${it.message}" }
            fetching = false
        }
    }

    fun connectRelay(info: RelayInfo, password: String? = null) {
        val key = info.address
        val pw = password ?: engine.rememberedPassword(key)
        if (info.password && pw.isEmpty()) {
            ask = PasswordAsk(info.name, key, false) { connectRelay(info, it) }
            return
        }
        (context as? com.ssjrocks.sdrtoolkit.MainActivity)?.askForNotifications()
        engine.connect(Source.RelaySource(info, pw))
    }

    fun connectSaved(s: SavedSource, password: String? = null) {
        (context as? com.ssjrocks.sdrtoolkit.MainActivity)?.askForNotifications()
        engine.connect(Source.Address(s.name, s.host, s.port, password ?: s.password))
    }

    LaunchedEffect(Unit) {
        SdrApp.usb.refresh()
        scan()
    }
    LaunchedEffect(driverError) {
        if (driverError == "NO_DRIVER") {
            needDriver = true
            onDriverErrorSeen()
        }
    }
    // A relay that wants a (better) password: ask, then try again with it.
    LaunchedEffect(conn) {
        if (conn.phase == ConnState.Phase.IDLE && conn.needsPassword) {
            val src = conn.source
            val retry = conn.error?.contains("wrong", ignoreCase = true) == true
            when (src) {
                is Source.RelaySource -> ask = PasswordAsk(src.info.name, src.info.address, retry) { connectRelay(src.info, it) }
                is Source.Address -> ask = PasswordAsk(src.label, "${src.host}:${src.port}", retry) { pw ->
                    saved.firstOrNull { it.host == src.host && it.port == src.port }?.let { engine.saveSource(it.copy(password = pw)) }
                    engine.connect(src.copy(password = pw))
                }
                else -> {}
            }
            engine.clearError()
        }
    }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        if (conn.phase == ConnState.Phase.IDLE && conn.error != null && !conn.needsPassword) {
            item {
                Notice(Icons.Filled.ErrorOutline, "Couldn't use ${conn.name}", conn.error!!, Palette.bad,
                    action = "OK", onAction = { engine.clearError() })
            }
        }
        if (driverError != null && driverError != "NO_DRIVER") {
            item {
                Notice(Icons.Filled.Usb, "The USB dongle didn't start", driverError, Palette.bad,
                    action = "OK", onAction = onDriverErrorSeen)
            }
        }

        // ---- USB ----
        item { Section(Icons.Filled.Usb, "USB dongles", "Plugged into this phone with an OTG adapter") }
        if (!hasUsbHost) {
            item { Hint("This phone doesn't support USB devices (no USB host). Use a relay instead.") }
        } else if (dongles.isEmpty()) {
            item {
                Hint("No RTL-SDR plugged in. Connect one through a USB-C or micro-USB OTG adapter; " +
                    "it appears here by itself.")
            }
        } else {
            items(dongles, key = { it.deviceName }) { d ->
                SourceCard(
                    icon = Icons.Filled.Usb,
                    title = d.name,
                    subtitle = if (d.supported) "RTL2832U · ${d.ids}" else "${d.ids} · only RTL-SDR dongles work for now",
                    enabled = d.supported,
                    active = conn.source is Source.Usb && (conn.source as Source.Usb).dongle == d.name &&
                        conn.phase != ConnState.Phase.IDLE,
                    onClick = {
                        if (!RtlDriver.isInstalled(context)) needDriver = true else onUseUsb(d)
                    },
                )
            }
        }

        // ---- LAN ----
        item {
            Section(Icons.Filled.Lan, "Relays on this network", "PCs running SDR Tools Relay",
                busy = scanning, action = Icons.Filled.Refresh, actionLabel = "Scan again", onAction = ::scan)
        }
        if (lan.isEmpty() && scanned && !scanning) {
            item {
                Hint("No relays answered. Is SDR Tools Relay running on the other PC, is this phone on the same " +
                    "Wi-Fi, and is UDP port ${Relay.DISCOVERY_PORT} allowed through its firewall? You can also " +
                    "add it by address below.")
            }
        }
        items(lan, key = { "lan:" + it.address }) { r -> RelayCard(r, conn) { connectRelay(r) } }

        // ---- Saved / by address ----
        item {
            Section(Icons.Filled.Router, "Saved addresses", "A relay or rtl_tcp server, by name or IP (Tailscale works)",
                action = Icons.Filled.Add, actionLabel = "Add", onAction = { adding = true })
        }
        if (saved.isEmpty()) {
            item {
                OutlinedButton(onClick = { adding = true }, Modifier.fillMaxWidth()) {
                    Icon(Icons.Filled.Add, null)
                    Spacer(Modifier.width(8.dp))
                    Text("Add a relay or rtl_tcp server by address")
                }
            }
        }
        items(saved, key = { "saved:" + it.key }) { s ->
            var menu by remember { mutableStateOf(false) }
            SourceCard(
                icon = Icons.Filled.Router,
                title = s.name.ifEmpty { s.host },
                subtitle = "${s.host}:${s.port}" + if (s.password.isNotEmpty()) " · password saved" else "",
                active = (conn.source as? Source.Address)?.let { it.host == s.host && it.port == s.port } == true &&
                    conn.phase != ConnState.Phase.IDLE,
                onClick = { connectSaved(s) },
                trailing = {
                    IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, "More") }
                    DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        DropdownMenuItem(text = { Text("Remove") }, leadingIcon = { Icon(Icons.Filled.Delete, null) },
                            onClick = { menu = false; engine.removeSource(s) })
                    }
                },
            )
        }

        // ---- Internet ----
        item {
            Section(Icons.Filled.Public, "Relays on the internet", "Listed on ssjrocks.com by their owners",
                busy = fetching, action = if (internet != null) Icons.Filled.Refresh else null, actionLabel = "Refresh",
                onAction = ::fetch)
        }
        if (internet == null && !fetching) {
            item {
                Column {
                    Hint("Streaming over the internet needs the bandwidth of the sample rate: about 20 Mbit/s at " +
                        "1.2 MS/s, 38 at 2.4 MS/s. Mobile data adds up fast.")
                    Spacer(Modifier.height(8.dp))
                    FilledTonalButton(onClick = ::fetch) {
                        Icon(Icons.Filled.Public, null)
                        Spacer(Modifier.width(8.dp))
                        Text("Show the list")
                    }
                }
            }
        }
        fetchError?.let { item { Hint(it, color = Palette.bad) } }
        internet?.let { list ->
            if (list.isEmpty()) item { Hint("No relays are listed at the moment.") }
            items(list, key = { "net:" + it.address }) { r -> RelayCard(r, conn) { connectRelay(r) } }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }

    ask?.let { a ->
        PasswordDialog(a, onDismiss = { ask = null }) { pw, remember ->
            ask = null
            if (remember) engine.rememberPassword(a.key, pw)
            a.connect(pw)
        }
    }
    if (adding) {
        AddressDialog(onDismiss = { adding = false }) { s ->
            adding = false
            engine.saveSource(s)
            connectSaved(s)
        }
    }
    if (needDriver) {
        AlertDialog(
            onDismissRequest = { needDriver = false },
            icon = { Icon(Icons.Filled.Usb, null) },
            title = { Text("Install the RTL-SDR driver") },
            text = {
                Text("Android apps reach USB dongles through a small free driver app, \"Rtl-sdr driver\" " +
                    "(the same one SDR Touch and RF Analyzer use). Install it once, then tap your dongle again. " +
                    "Android will ask whether the driver may use the dongle: allow it.")
            },
            confirmButton = {
                TextButton(onClick = { needDriver = false; RtlDriver.openStore(context) }) { Text("Get the driver") }
            },
            dismissButton = { TextButton(onClick = { needDriver = false }) { Text("Not now") } },
        )
    }
}

@Composable
private fun Section(icon: ImageVector, title: String, subtitle: String, busy: Boolean = false,
                    action: ImageVector? = null, actionLabel: String = "", onAction: () -> Unit = {}) {
    Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Icon(icon, null, tint = Palette.accent)
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Text(subtitle, style = MaterialTheme.typography.bodySmall, color = Palette.textDim)
        }
        if (busy) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp)
        else if (action != null) IconButton(onClick = onAction) { Icon(action, actionLabel) }
    }
}

@Composable
fun Hint(text: String, color: androidx.compose.ui.graphics.Color = Palette.textDim) {
    Text(text, style = MaterialTheme.typography.bodyMedium, color = color, modifier = Modifier.padding(start = 36.dp))
}

@Composable
fun Notice(icon: ImageVector, title: String, text: String, color: androidx.compose.ui.graphics.Color,
           action: String? = null, onAction: () -> Unit = {}) {
    Card(colors = CardDefaults.cardColors(containerColor = Palette.surfaceHigh)) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.Top) {
            Icon(icon, null, tint = color)
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(4.dp))
                Text(text, style = MaterialTheme.typography.bodyMedium, color = Palette.textDim)
            }
            if (action != null) TextButton(onClick = onAction) { Text(action) }
        }
    }
}

@Composable
private fun SourceCard(icon: ImageVector, title: String, subtitle: String, active: Boolean, onClick: () -> Unit,
                       enabled: Boolean = true, badges: @Composable () -> Unit = {},
                       trailing: (@Composable () -> Unit)? = null) {
    Card(
        colors = CardDefaults.cardColors(
            containerColor = if (active) Palette.accentDim.copy(alpha = 0.35f) else Palette.surface,
        ),
    ) {
        // The click is on the ListItem, so its text and the action are one node for TalkBack.
        ListItem(
            modifier = Modifier.clickable(enabled = enabled, onClick = onClick),
            colors = ListItemDefaults.colors(containerColor = androidx.compose.ui.graphics.Color.Transparent),
            leadingContent = { Icon(icon, null, tint = if (active) Palette.accent else Palette.textDim) },
            headlineContent = {
                Text(title, fontWeight = FontWeight.SemiBold, color = if (enabled) Palette.text else Palette.textDim)
            },
            supportingContent = {
                Column {
                    Text(subtitle, color = Palette.textDim)
                    badges()
                }
            },
            trailingContent = trailing ?: if (active) ({ Text("In use", color = Palette.accent) }) else null,
        )
    }
}

@Composable
private fun RelayCard(r: RelayInfo, conn: ConnState, onClick: () -> Unit) {
    val active = (conn.source as? Source.RelaySource)?.info?.address == r.address && conn.phase != ConnState.Phase.IDLE
    SourceCard(
        icon = if (r.origin == RelayInfo.Origin.INTERNET) Icons.Filled.Public else Icons.Filled.Lan,
        title = r.name,
        subtitle = listOfNotNull(
            r.location.ifEmpty { null },
            r.address,
            r.devices.joinToString(", ").ifEmpty { "no dongle" },
        ).joinToString(" · "),
        active = active,
        onClick = onClick,
        badges = {
            if (r.password || r.busy || r.version.isNotEmpty()) {
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.padding(top = 6.dp)) {
                    if (r.password) Badge("Password", Icons.Filled.Lock)
                    if (r.busy && !active) Badge("In use", null, Palette.warn)
                    if (r.version.isNotEmpty()) Badge("v${r.version}", null)
                }
            }
        },
    )
}

@Composable
private fun Badge(text: String, icon: ImageVector?, color: androidx.compose.ui.graphics.Color = Palette.textDim) {
    Row(
        Modifier.border(1.dp, Palette.surfaceHigh, RoundedCornerShape(8.dp)).padding(horizontal = 8.dp, vertical = 3.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (icon != null) {
            Icon(icon, null, Modifier.size(13.dp), tint = color)
            Spacer(Modifier.width(4.dp))
        }
        Text(text, style = MaterialTheme.typography.labelSmall, color = color)
    }
}

@Composable
private fun PasswordDialog(a: PasswordAsk, onDismiss: () -> Unit, onConnect: (String, Boolean) -> Unit) {
    var pw by remember { mutableStateOf("") }
    var keep by remember { mutableStateOf(true) }
    AlertDialog(
        onDismissRequest = onDismiss,
        icon = { Icon(Icons.Filled.Lock, null) },
        title = { Text(a.title) },
        text = {
            Column {
                Text(if (a.retry) "That password didn't work. Try again:" else "This relay has a password.",
                    color = if (a.retry) Palette.bad else Palette.textDim)
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(pw, { pw = it }, label = { Text("Password") }, singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(keep, { keep = it })
                    Text("Remember it on this phone")
                }
                Text("The password never crosses the network: the relay checks a hash of it.",
                    style = MaterialTheme.typography.bodySmall, color = Palette.textDim)
            }
        },
        confirmButton = { TextButton(onClick = { onConnect(pw, keep) }, enabled = pw.isNotEmpty()) { Text("Connect") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun AddressDialog(onDismiss: () -> Unit, onSave: (SavedSource) -> Unit) {
    var name by remember { mutableStateOf("") }
    var address by remember { mutableStateOf("") }
    var pw by remember { mutableStateOf("") }
    val parsed = Relay.parseAddress(address)
    AlertDialog(
        onDismissRequest = onDismiss,
        icon = { Icon(Icons.Filled.Router, null) },
        title = { Text("Add by address") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(address, { address = it.trim() }, label = { Text("Address") },
                    placeholder = { Text("host or IP[:port]") }, singleLine = true,
                    supportingText = { Text("Port ${Relay.PORT} if left out. An rtl_tcp server works too (usually :1234).") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
                OutlinedTextField(name, { name = it }, label = { Text("Name (optional)") }, singleLine = true)
                OutlinedTextField(pw, { pw = it }, label = { Text("Password (if it has one)") }, singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password))
            }
        },
        confirmButton = {
            TextButton(enabled = parsed != null, onClick = {
                parsed?.let { (h, p) -> onSave(SavedSource(name.trim(), h, p, pw)) }
            }) { Text("Save and connect") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
