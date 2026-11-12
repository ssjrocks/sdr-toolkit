package com.ssjrocks.sdrtoolkit

import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.compose.ui.test.ExperimentalTestApi
import androidx.compose.ui.test.hasContentDescription
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.performSemanticsAction
import com.ssjrocks.sdrtoolkit.core.Relay
import com.ssjrocks.sdrtoolkit.engine.ConnState
import com.ssjrocks.sdrtoolkit.engine.Task
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

/**
 * The whole app on the JVM: found a relay by scanning, logged in with its password, the band on
 * the receiver, the settings and bands sheets, then aircraft tracked from the relay's ADS-B. The
 * relay is SimRelay, making up its signals in real time. Screenshots go to -DscreenshotDir.
 */
@OptIn(ExperimentalTestApi::class)
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [35], qualifiers = "w400dp-h860dp-xhdpi")
class UiWalkthroughTest {
    @get:Rule
    val compose = createAndroidComposeRule<MainActivity>()
    private val relay = SimRelay(password = "pw")
    private val shots = System.getProperty("screenshotDir")?.let { File(it).apply { mkdirs() } }

    @After
    fun tearDown() {
        SdrApp.engine.disconnect()
        relay.close()
    }

    private fun shot(name: String) {
        compose.waitForIdle()
        shotNoIdle(name)
    }

    /** Draws the window (dialogs and sheets are windows of their own: the top one) into a PNG. */
    private fun shotNoIdle(name: String) {
        val dir = shots ?: return
        compose.activityRule.scenario.onActivity { act ->
            val root = act.window.decorView
            val bmp = Bitmap.createBitmap(root.width, root.height, Bitmap.Config.ARGB_8888)
            val canvas = Canvas(bmp)
            root.draw(canvas)
            // Dialogs and bottom sheets: every other window of the app, drawn over it.
            for (v in WindowManagerGlobalAccess.views()) {
                if (v === root || !v.isShown) continue
                val loc = IntArray(2)
                v.getLocationOnScreen(loc)
                canvas.save()
                canvas.translate(loc[0].toFloat(), loc[1].toFloat())
                if (v.rootView.layoutParams.let { it is android.view.WindowManager.LayoutParams && it.dimAmount > 0 &&
                        (it.flags and android.view.WindowManager.LayoutParams.FLAG_DIM_BEHIND) != 0 }) {
                    canvas.drawColor(android.graphics.Color.argb(150, 0, 0, 0))
                }
                v.draw(canvas)
                canvas.restore()
            }
            File(dir, "$name.png").outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
        }
    }

    private fun waitFor(what: String, timeoutMs: Long = 15_000, condition: () -> Boolean) {
        try {
            compose.waitUntil(timeoutMs) { condition() }
        } catch (e: Throwable) {
            throw AssertionError("timed out waiting for $what (connection: ${SdrApp.engine.conn.value})", e)
        }
    }

    /** Runs the UI for [n] frames without waiting for Compose to be idle (it isn't, with a dialog up, under Robolectric). */
    private fun frames(n: Int = 20) {
        repeat(n) {
            compose.mainClock.advanceTimeByFrame()
            org.robolectric.Shadows.shadowOf(android.os.Looper.getMainLooper()).idle()
        }
    }

    /** Back, on the top dialog or sheet: it closes as the user would close it. */
    private fun back() {
        val d = org.robolectric.shadows.ShadowDialog.getLatestDialog() as androidx.activity.ComponentDialog
        d.onBackPressedDispatcher.onBackPressed()
        frames(40)
    }

    /** Clicks the clickable thing labelled [text] (through its click action, as accessibility services do). */
    private fun click(text: String) {
        compose.onAllNodes(hasText(text) and hasClickAction())[0].performSemanticsAction(SemanticsActions.OnClick)
    }

    private fun clickDescribed(description: String) {
        compose.onAllNodes(hasContentDescription(description) and hasClickAction())[0]
            .performSemanticsAction(SemanticsActions.OnClick)
    }

    @Test
    fun walkthrough() {
        val engine = SdrApp.engine
        // ---- Devices: the relay turns up from the LAN scan ----
        waitFor("the relay in the scan") { compose.onAllNodes(hasText("Shack PC")).fetchSemanticsNodes().isNotEmpty() }
        shot("1-devices")

        // ---- it has a password: the dialog asks for it ----
        click("Shack PC")
        frames()
        assertEquals("the password dialog", 2, WindowManagerGlobalAccess.views().size)
        shotNoIdle("2-password")
        back()
        assertEquals(1, WindowManagerGlobalAccess.views().size)
        // What the dialog's "Remember it" does; then a tap connects straight away.
        engine.rememberPassword(Relay.discover(1500).first { it.port == relay.port }.address, "pw")
        click("Shack PC")

        // ---- straight to the receiver, the band on screen ----
        waitFor("streaming") { engine.conn.value.phase == ConnState.Phase.STREAMING }
        assertTrue(engine.conn.value.isRelay)
        assertEquals("R828D", engine.conn.value.tuner)
        waitFor("the waterfall to fill", 30_000) { (engine.spectrum.value?.seq ?: 0) > 120 }
        waitFor("the receiver tab") { compose.onAllNodes(hasText("Step", substring = true)).fetchSemanticsNodes().isNotEmpty() }
        assertTrue("a station at -6 dB or so", engine.stats.value.levelDb > -20)
        shot("3-receiver")

        // ---- tap to another station: the one 600 kHz up is at 101.1 MHz ----
        clickDescribed("Bands")
        frames(40)
        assertEquals("the bands sheet", 2, WindowManagerGlobalAccess.views().size)
        shotNoIdle("4-bands")
        back()

        clickDescribed("Receiver settings")
        frames(40)
        assertEquals("the settings sheet", 2, WindowManagerGlobalAccess.views().size)
        shotNoIdle("5-settings")
        back()

        // ---- aircraft ----
        engine.update { it.copy(myLat = 52.3086, myLon = 4.7639) }       // Schiphol
        click("Aircraft")
        waitFor("the start button") { compose.onAllNodes(hasText("Start tracking")).fetchSemanticsNodes().isNotEmpty() }
        shot("6-aircraft-start")
        click("Start tracking")
        assertEquals(Task.ADSB, engine.task.value)
        waitFor("all five aircraft with positions", 30_000) {
            engine.aircraft.value.count { it.lat != null && it.callsign.isNotEmpty() } == relay.planes.size
        }
        compose.mainClock.advanceTimeBy(1500)
        waitFor("the list") { compose.onAllNodes(hasText("KLM1023")).fetchSemanticsNodes().isNotEmpty() }
        val klm = engine.aircraft.value.first { it.callsign == "KLM1023" }
        assertEquals(52.42, klm.lat!!, 0.001)
        assertEquals(4.62, klm.lon!!, 0.001)
        assertEquals(23000, klm.altitudeFt)
        assertEquals(410.0, klm.speedKt!!, 1.0)
        shot("7-aircraft")

        // ---- back to the receiver, where it was ----
        click("Receiver")
        compose.waitForIdle()
        assertEquals(Task.RECEIVER, engine.task.value)
    }
}

/** The app's windows (activity, dialogs, sheets), from Android's WindowManagerGlobal. */
private object WindowManagerGlobalAccess {
    fun views(): List<android.view.View> {
        val wmg = Class.forName("android.view.WindowManagerGlobal")
        val inst = wmg.getMethod("getInstance").invoke(null)
        val f = wmg.getDeclaredField("mViews").apply { isAccessible = true }
        @Suppress("UNCHECKED_CAST")
        return (f.get(inst) as List<android.view.View>).toList()
    }
}
