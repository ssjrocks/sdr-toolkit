package com.ssjrocks.sdrtoolkit.engine

import android.content.ActivityNotFoundException
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.net.Uri
import androidx.core.content.ContextCompat
import com.ssjrocks.sdrtoolkit.core.KnownDongles
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** A USB device plugged into the phone that looks like a software radio. */
data class UsbDongle(
    val deviceName: String,
    val name: String,
    val vendorId: Int,
    val productId: Int,
    val supported: Boolean,
) {
    val ids get() = "%04x:%04x".format(vendorId, productId)
}

/** Watches the USB port (through an OTG adapter) for RTL-SDR dongles. */
class UsbDongles(private val context: Context) {
    private val usb = context.getSystemService(UsbManager::class.java)
    private val _list = MutableStateFlow<List<UsbDongle>>(emptyList())
    val list: StateFlow<List<UsbDongle>> = _list.asStateFlow()

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) = refresh()
    }

    init {
        val filter = IntentFilter().apply {
            addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED)
            addAction(UsbManager.ACTION_USB_DEVICE_DETACHED)
        }
        ContextCompat.registerReceiver(context, receiver, filter, ContextCompat.RECEIVER_NOT_EXPORTED)
        refresh()
    }

    fun refresh() {
        val devices = try {
            usb?.deviceList?.values?.toList() ?: emptyList()
        } catch (_: Exception) {
            emptyList()
        }
        _list.value = devices.mapNotNull(::describe).sortedBy { !it.supported }
    }

    private fun describe(d: UsbDevice): UsbDongle? {
        val known = KnownDongles.name(d.vendorId, d.productId)
        val other = OTHER_SDRS[(d.vendorId shl 16) or d.productId]
        if (known == null && other == null) return null
        val product = listOfNotNull(d.manufacturerName, d.productName).joinToString(" ").trim()
        return UsbDongle(d.deviceName, product.ifEmpty { known ?: other!! }, d.vendorId, d.productId, known != null)
    }

    companion object {
        /** Radios that aren't RTL-SDRs: listed, so it's clear why they can't be used. */
        private val OTHER_SDRS = mapOf(
            0x1d506089 to "HackRF One",
            0x1d5060a1 to "Airspy",
            0x03eb800c to "Airspy HF+",
            0x1df73000 to "SDRplay RSP1",
            0x1df73010 to "SDRplay RSP2",
            0x1df73020 to "SDRplay RSPduo",
        )
    }
}

/**
 * The RTL-SDR driver app (rtl_tcp_andro, "Rtl-sdr driver" on Google Play): it takes the USB
 * dongle and serves it as rtl_tcp on localhost, started with an iqsrc:// intent. SDR Touch and
 * RF Analyzer use it the same way.
 */
object RtlDriver {
    const val PACKAGE = "marto.rtl_tcp_andro"
    const val PORT = 14423

    /** The driver's arguments are rtl_tcp's (SdrTcpArguments: -a -p -s -f, -P ppm, -T bias tee). */
    fun intent(rate: Int, freqHz: Long, ppm: Int = 0, biasTee: Boolean = false, port: Int = PORT): Intent =
        Intent(Intent.ACTION_VIEW, Uri.parse(
            "iqsrc://-a 127.0.0.1 -p $port -s $rate -f $freqHz -P $ppm -T ${if (biasTee) 1 else 0}"))

    fun isInstalled(context: Context): Boolean = try {
        context.packageManager.getPackageInfo(PACKAGE, 0)
        true
    } catch (_: PackageManager.NameNotFoundException) {
        context.packageManager.queryIntentActivities(intent(2_400_000, 100_000_000), 0).isNotEmpty()
    }

    fun openStore(context: Context) {
        val market = Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=$PACKAGE"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        try {
            context.startActivity(market)
        } catch (_: ActivityNotFoundException) {
            context.startActivity(Intent(Intent.ACTION_VIEW,
                Uri.parse("https://play.google.com/store/apps/details?id=$PACKAGE")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }

    /** Why the driver didn't start, from its result (the id is its SdrException.err_info). */
    fun error(data: Intent?): String {
        val detail = data?.getStringExtra("detailed_exception_message")
        val id = data?.getIntExtra("marto.rtl_tcp_andro.RtlTcpExceptionId", -1) ?: -1
        val reason = when (id) {
            0 -> "the driver wasn't allowed to use the USB device"
            1 -> "the driver needs root on this phone"
            2 -> "no RTL-SDR found: is it plugged in through an OTG adapter?"
            4 -> "the dongle needs unplugging and plugging back in"
            5 -> "the driver is already running for another app"
            else -> "the driver stopped (or was cancelled)"
        }
        return if (detail.isNullOrBlank()) reason else "$reason ($detail)"
    }
}
