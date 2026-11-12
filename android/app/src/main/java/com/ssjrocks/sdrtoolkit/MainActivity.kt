package com.ssjrocks.sdrtoolkit

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.usb.UsbManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Flight
import androidx.compose.material.icons.filled.GraphicEq
import androidx.compose.material.icons.filled.SettingsInputAntenna
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.ssjrocks.sdrtoolkit.engine.ConnState
import com.ssjrocks.sdrtoolkit.engine.RtlDriver
import com.ssjrocks.sdrtoolkit.engine.Source
import com.ssjrocks.sdrtoolkit.engine.Task
import com.ssjrocks.sdrtoolkit.engine.UsbDongle
import com.ssjrocks.sdrtoolkit.ui.AircraftScreen
import com.ssjrocks.sdrtoolkit.ui.DevicesScreen
import com.ssjrocks.sdrtoolkit.ui.Palette
import com.ssjrocks.sdrtoolkit.ui.ReceiverScreen
import com.ssjrocks.sdrtoolkit.ui.SdrTheme
import java.util.Locale

enum class Tab(val label: String) { DEVICES("Devices"), RECEIVER("Receiver"), AIRCRAFT("Aircraft") }

class MainActivity : ComponentActivity() {
    private val engine get() = SdrApp.engine
    private var pendingDongle: UsbDongle? = null
    private val tabRequest = mutableStateOf<Tab?>(null)
    private val driverError = mutableStateOf<String?>(null)
    private val locating = mutableStateOf(false)

    private val driver = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val dongle = pendingDongle ?: return@registerForActivityResult
        pendingDongle = null
        if (result.resultCode == Activity.RESULT_OK) {
            engine.connect(Source.Usb(dongle.name, RtlDriver.PORT))
        } else {
            driverError.value = RtlDriver.error(result.data)
        }
    }

    private val notifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    private val locationPermission = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { r ->
        if (r.values.any { it }) fetchLocation() else locating.value = false
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        handleIntent(intent)
        setContent {
            SdrTheme {
                App(
                    tabRequest = tabRequest.value,
                    onTabHandled = { tabRequest.value = null },
                    driverError = driverError.value,
                    onDriverErrorSeen = { driverError.value = null },
                    onUseUsb = ::useUsb,
                    locating = locating.value,
                    onLocate = ::requestLocation,
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        if (intent?.action == UsbManager.ACTION_USB_DEVICE_ATTACHED) {
            SdrApp.usb.refresh()
            tabRequest.value = Tab.DEVICES
        }
    }

    /** Starts the RTL-SDR driver for the dongle; it answers in [driver]. */
    private fun useUsb(dongle: UsbDongle) {
        askForNotifications()
        val s = engine.settings.value
        engine.disconnect()
        pendingDongle = dongle
        try {
            driver.launch(RtlDriver.intent(s.sampleRate, s.centerHz, s.ppm, s.biasTee))
        } catch (e: Exception) {
            pendingDongle = null
            driverError.value = "NO_DRIVER"
        }
    }

    fun askForNotifications() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun requestLocation() {
        locating.value = true
        val granted = listOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
            .any { ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED }
        if (granted) fetchLocation() else locationPermission.launch(
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION))
    }

    /** A recent last-known fix if there is one, else the next one (giving up after 20 s). */
    @SuppressLint("MissingPermission")
    private fun fetchLocation() {
        val lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        fun use(l: Location?) {
            locating.value = false
            if (l != null) engine.update { it.copy(myLat = l.latitude, myLon = l.longitude) }
        }
        val providers = try { lm.getProviders(true) } catch (_: SecurityException) { emptyList() }
        val last = providers.mapNotNull { p -> runCatching { lm.getLastKnownLocation(p) }.getOrNull() }
            .maxByOrNull { it.time }
        if (last != null && System.currentTimeMillis() - last.time < 10 * 60 * 1000) return use(last)
        val provider = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER).firstOrNull { it in providers }
            ?: return use(last)
        val handler = Handler(Looper.getMainLooper())
        val listener = object : LocationListener {
            override fun onLocationChanged(location: Location) {
                lm.removeUpdates(this)
                handler.removeCallbacksAndMessages(null)
                use(location)
            }
        }
        try {
            lm.requestLocationUpdates(provider, 0L, 0f, listener, Looper.getMainLooper())
            handler.postDelayed({ lm.removeUpdates(listener); use(last) }, 20_000)
        } catch (_: SecurityException) {
            use(last)
        }
    }
}

@Composable
private fun App(
    tabRequest: Tab?,
    onTabHandled: () -> Unit,
    driverError: String?,
    onDriverErrorSeen: () -> Unit,
    onUseUsb: (UsbDongle) -> Unit,
    locating: Boolean,
    onLocate: () -> Unit,
) {
    val engine = SdrApp.engine
    val conn by engine.conn.collectAsStateWithLifecycle()
    val task by engine.task.collectAsStateWithLifecycle()
    var tab by rememberSaveable { mutableStateOf(Tab.DEVICES) }
    var connectingFromDevices by remember { mutableStateOf(false) }

    LaunchedEffect(tabRequest) {
        if (tabRequest != null) {
            tab = tabRequest
            onTabHandled()
        }
    }
    // Straight to the receiver (or the aircraft) once a connection made on the Devices tab is up.
    LaunchedEffect(conn.phase) {
        if (conn.phase == ConnState.Phase.CONNECTING) connectingFromDevices = tab == Tab.DEVICES
        if (conn.phase == ConnState.Phase.STREAMING && connectingFromDevices) {
            tab = if (task == Task.ADSB) Tab.AIRCRAFT else Tab.RECEIVER
            connectingFromDevices = false
        }
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = { StatusStrip(conn, onOpenDevices = { tab = Tab.DEVICES }) },
        bottomBar = {
            NavigationBar(containerColor = Palette.surface) {
                for (t in Tab.entries) {
                    NavigationBarItem(
                        selected = tab == t,
                        onClick = { tab = t },
                        icon = {
                            Icon(when (t) {
                                Tab.DEVICES -> Icons.Filled.SettingsInputAntenna
                                Tab.RECEIVER -> Icons.Filled.GraphicEq
                                Tab.AIRCRAFT -> Icons.Filled.Flight
                            }, contentDescription = null)
                        },
                        label = { Text(t.label) },
                    )
                }
            }
        },
    ) { padding ->
        Box(Modifier.padding(padding).fillMaxSize()) {
            when (tab) {
                Tab.DEVICES -> DevicesScreen(onUseUsb = onUseUsb, driverError = driverError,
                    onDriverErrorSeen = onDriverErrorSeen)
                Tab.RECEIVER -> ReceiverScreen(onOpenDevices = { tab = Tab.DEVICES },
                    onOpenAircraft = { tab = Tab.AIRCRAFT })
                Tab.AIRCRAFT -> AircraftScreen(onOpenDevices = { tab = Tab.DEVICES }, locating = locating,
                    onLocate = onLocate)
            }
        }
    }
}

/** What's connected, at the top of every tab: a dot, the source and its data rate. */
@Composable
private fun StatusStrip(conn: ConnState, onOpenDevices: () -> Unit) {
    val engine = SdrApp.engine
    val stats by engine.stats.collectAsStateWithLifecycle()
    Surface(color = Palette.surface) {
        Column(Modifier.fillMaxWidth().statusBarsPadding()) {
            Row(
                Modifier.fillMaxWidth().clickable(onClick = onOpenDevices).padding(horizontal = 16.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                val (color, text) = when (conn.phase) {
                    ConnState.Phase.STREAMING -> Palette.good to conn.name
                    ConnState.Phase.CONNECTING -> Palette.warn to "Connecting to ${conn.name}…"
                    ConnState.Phase.IDLE -> (if (conn.error != null) Palette.bad else Palette.textDim) to
                        (if (conn.error != null) "Not connected: tap for details" else "No radio: tap to connect one")
                }
                Box(Modifier.size(10.dp).clip(CircleShape).background(color))
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    Text(text, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                    if (conn.phase == ConnState.Phase.STREAMING) {
                        val kind = when (conn.source) {
                            is Source.Usb -> "USB"
                            else -> if (conn.isRelay) (if (conn.onLan) "LAN relay" else "Internet relay") else "rtl_tcp"
                        }
                        val rate = String.format(Locale.US, "%.1f MB/s", stats.kbytesPerSec / 1024)
                        Text("$kind · ${conn.tuner} · $rate" + if (stats.dropped > 0) " · ${stats.dropped} dropped" else "",
                            style = MaterialTheme.typography.bodySmall, color = Palette.textDim, maxLines = 1)
                    }
                }
                if (conn.phase != ConnState.Phase.IDLE) {
                    IconButton(onClick = { engine.disconnect() }) {
                        Icon(Icons.Filled.Close, contentDescription = "Disconnect")
                    }
                }
            }
            AnimatedVisibility(conn.phase == ConnState.Phase.CONNECTING) {
                LinearProgressIndicator(Modifier.fillMaxWidth())
            }
        }
    }
}
