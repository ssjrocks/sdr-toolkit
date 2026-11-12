package com.ssjrocks.sdrtoolkit.engine

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.ssjrocks.sdrtoolkit.MainActivity
import com.ssjrocks.sdrtoolkit.R
import com.ssjrocks.sdrtoolkit.SdrApp
import com.ssjrocks.sdrtoolkit.ui.formatMHz
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch

/**
 * Keeps the radio going while the app is in the background or the screen is off: a foreground
 * service with a notification (what's playing, and Disconnect), and Wi-Fi and CPU locks.
 */
class SdrService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var job: Job? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_DISCONNECT) {
            SdrApp.engine.disconnect()
            stopSelf()
            return START_NOT_STICKY
        }
        val n = build("Connecting…")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(ID, n)
        }
        acquireLocks()
        if (job == null) {
            val e = SdrApp.engine
            job = scope.launch {
                combine(e.conn, e.settings, e.task, e.adsb) { c, s, t, a ->
                    val what = if (t == Task.ADSB) "ADS-B · ${a.aircraft} aircraft" else
                        "${formatMHz(s.tunedHz)} MHz · ${s.mode.label}"
                    "${c.name}: $what"
                }.distinctUntilChanged().collect { text ->
                    getSystemService(NotificationManager::class.java).notify(ID, build(text))
                    delay(1000)
                }
            }
        }
        return START_NOT_STICKY
    }

    private fun build(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 1, Intent(this, SdrService::class.java).setAction(ACTION_DISCONNECT),
            PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("SDR Toolkit")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .addAction(0, "Disconnect", stop)
            .build()
    }

    @Suppress("DEPRECATION")
    private fun acquireLocks() {
        if (wifiLock == null) {
            val wm = applicationContext.getSystemService(WifiManager::class.java)
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            else WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifiLock = wm?.createWifiLock(mode, "sdr-toolkit:stream")?.apply { setReferenceCounted(false); acquire() }
        }
        if (wakeLock == null) {
            val pm = getSystemService(PowerManager::class.java)
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "sdr-toolkit:dsp").apply {
                setReferenceCounted(false)
                acquire(12 * 60 * 60 * 1000L)
            }
        }
    }

    override fun onDestroy() {
        scope.cancel()
        wifiLock?.release()
        wakeLock?.release()
        super.onDestroy()
    }

    companion object {
        private const val ID = 1
        const val CHANNEL = "radio"
        private const val ACTION_DISCONNECT = "com.ssjrocks.sdrtoolkit.DISCONNECT"

        fun createChannel(context: Context) {
            val ch = NotificationChannel(CHANNEL, "Radio running", NotificationManager.IMPORTANCE_LOW)
            ch.description = "Shown while a dongle or relay is connected"
            context.getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }

        fun start(context: Context) {
            try {
                ContextCompat.startForegroundService(context, Intent(context, SdrService::class.java))
            } catch (e: Exception) {      // started from the background (Android 12+): carry on without it
                Log.i("SdrService", "not started: $e")
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, SdrService::class.java))
        }
    }
}
