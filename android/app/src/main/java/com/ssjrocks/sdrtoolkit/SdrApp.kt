package com.ssjrocks.sdrtoolkit

import android.app.Application
import com.ssjrocks.sdrtoolkit.core.Relay
import com.ssjrocks.sdrtoolkit.engine.SdrEngine
import com.ssjrocks.sdrtoolkit.engine.SdrService
import com.ssjrocks.sdrtoolkit.engine.UsbDongles

class SdrApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Relay.clientVersion = BuildConfig.VERSION_NAME
        SdrService.createChannel(this)
        engine = SdrEngine(this)
        usb = UsbDongles(this)
    }

    companion object {
        lateinit var engine: SdrEngine
            private set
        lateinit var usb: UsbDongles
            private set
    }
}
