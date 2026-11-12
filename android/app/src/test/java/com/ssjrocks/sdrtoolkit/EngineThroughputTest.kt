package com.ssjrocks.sdrtoolkit

import com.ssjrocks.sdrtoolkit.core.Relay
import com.ssjrocks.sdrtoolkit.engine.ConnState
import com.ssjrocks.sdrtoolkit.engine.Source
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** The engine keeps up with a relay at 2.4 MS/s: nothing dropped, the spectrum at 25 frames a second. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class EngineThroughputTest {
    @Test
    fun keepsUp() {
        SimRelay(name = "Bench", discoveryPort = 0).use { relay ->
            val engine = SdrApp.engine
            engine.update { it.copy(sampleRate = 2_400_000, tunedHz = 101_100_000, centerHz = 100_500_000) }
            val info = Relay.check("127.0.0.1", relay.port, "")
            engine.connect(Source.RelaySource(info.copy(host = "127.0.0.1"), ""))
            val end = System.currentTimeMillis() + 10_000
            while (engine.conn.value.phase != ConnState.Phase.STREAMING && System.currentTimeMillis() < end) Thread.sleep(50)
            assertEquals(ConnState.Phase.STREAMING, engine.conn.value.phase)
            // The simulated band takes a moment to make (twice: the first rate, then 2.4 MS/s).
            val first = System.currentTimeMillis() + 15_000
            while ((engine.spectrum.value?.seq ?: 0) < 50 && System.currentTimeMillis() < first) Thread.sleep(50)
            Thread.sleep(1000)                       // JIT warm-up
            val seq0 = engine.spectrum.value!!.seq
            val dropped0 = engine.stats.value.dropped
            Thread.sleep(6000)
            val frames = engine.spectrum.value!!.seq - seq0
            val dropped = engine.stats.value.dropped - dropped0
            println("THROUGHPUT: ${engine.stats.value.kbytesPerSec} kB/s, $frames frames in 6 s, $dropped dropped (${dropped0} while warming up)")
            engine.disconnect()
            assertTrue("frames $frames", frames in 140..160)
            assertEquals(0L, dropped)
        }
    }
}
