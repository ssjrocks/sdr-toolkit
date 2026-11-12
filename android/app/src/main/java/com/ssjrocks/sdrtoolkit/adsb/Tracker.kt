package com.ssjrocks.sdrtoolkit.adsb

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.asin
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.sin
import kotlin.math.sqrt

/** One aircraft, as the list shows it. */
data class Aircraft(
    val icao: Int,
    val callsign: String = "",
    val squawk: String = "",
    val altitudeFt: Int? = null,
    val speedKt: Double? = null,
    val track: Double? = null,
    val verticalRateFpm: Int? = null,
    val lat: Double? = null,
    val lon: Double? = null,
    val onGround: Boolean = false,
    val messages: Int = 0,
    val signalDbfs: Double = -99.0,
    val lastSeenMs: Long = 0,
    val lastPositionMs: Long = 0,
    val category: Int = 0,
) {
    val hex: String get() = "%06X".format(icao)
}

/** CPR (compact position reporting) for airborne positions, as DO-260 describes it. */
object Cpr {
    private const val NZ = 15
    private const val SCALE = 131072.0

    fun nl(lat: Double): Int {
        val a = abs(lat)
        if (a < 1e-9) return 59
        if (abs(a - 87) < 1e-9) return 2
        if (a > 87) return 1
        val c = cos(PI / 180 * a)
        return floor(2 * PI / acos(1 - (1 - cos(PI / (2 * NZ))) / (c * c))).toInt()
    }

    private fun mod(a: Double, b: Double): Double {
        val r = a % b
        return if (r < 0) r + b else r
    }

    /** From an even and an odd message; the newer one's position. Null if they straddle a zone edge. */
    fun global(evenLat: Int, evenLon: Int, oddLat: Int, oddLon: Int, oddNewer: Boolean): Pair<Double, Double>? {
        val dLatE = 360.0 / 60
        val dLatO = 360.0 / 59
        val latE = evenLat / SCALE
        val latO = oddLat / SCALE
        val lonE = evenLon / SCALE
        val lonO = oddLon / SCALE
        val j = floor(59 * latE - 60 * latO + 0.5)
        var rlatE = dLatE * (mod(j, 60.0) + latE)
        var rlatO = dLatO * (mod(j, 59.0) + latO)
        if (rlatE >= 270) rlatE -= 360
        if (rlatO >= 270) rlatO -= 360
        if (rlatE < -90 || rlatE > 90 || rlatO < -90 || rlatO > 90) return null
        if (nl(rlatE) != nl(rlatO)) return null
        val lat = if (oddNewer) rlatO else rlatE
        val nl = nl(lat)
        val ni = maxOf(if (oddNewer) nl - 1 else nl, 1)
        val m = floor(lonE * (nl - 1) - lonO * nl + 0.5)
        var lon = (360.0 / ni) * (mod(m, ni.toDouble()) + if (oddNewer) lonO else lonE)
        if (lon >= 180) lon -= 360
        return lat to lon
    }

    /** From one message and a position within 180 NM of it (an earlier position, or the receiver). */
    fun local(latCpr: Int, lonCpr: Int, odd: Boolean, refLat: Double, refLon: Double): Pair<Double, Double> {
        val dLat = 360.0 / if (odd) 59 else 60
        val y = latCpr / SCALE
        val j = floor(refLat / dLat) + floor(0.5 + mod(refLat, dLat) / dLat - y)
        val lat = dLat * (j + y)
        val ni = nl(lat) - (if (odd) 1 else 0)
        val dLon = 360.0 / maxOf(ni, 1)
        val x = lonCpr / SCALE
        val m = floor(refLon / dLon) + floor(0.5 + mod(refLon, dLon) / dLon - x)
        var lon = dLon * (m + x)
        if (lon >= 180) lon -= 360
        if (lon < -180) lon += 360
        return lat to lon
    }
}

object Geo {
    /** Great-circle distance in km. */
    fun distanceKm(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val p1 = Math.toRadians(lat1)
        val p2 = Math.toRadians(lat2)
        val dp = p2 - p1
        val dl = Math.toRadians(lon2 - lon1)
        val a = sin(dp / 2) * sin(dp / 2) + cos(p1) * cos(p2) * sin(dl / 2) * sin(dl / 2)
        return 6371.0 * 2 * asin(sqrt(a.coerceIn(0.0, 1.0)))
    }

    /** Initial bearing from point 1 to point 2, degrees from north. */
    fun bearing(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val p1 = Math.toRadians(lat1)
        val p2 = Math.toRadians(lat2)
        val dl = Math.toRadians(lon2 - lon1)
        val y = sin(dl) * cos(p2)
        val x = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)
        return (Math.toDegrees(atan2(y, x)) + 360) % 360
    }
}

/**
 * Keeps the aircraft heard: decodes each message, pairs even and odd positions, and drops
 * aircraft not heard for [ttlMs]. Not thread-safe: one thread feeds it, [snapshot] copies out.
 */
class Tracker(var ttlMs: Long = 60_000, private val clock: () -> Long = System::currentTimeMillis) {
    private class Cpr1(val lat: Int, val lon: Int, val t: Long)

    private class State(var a: Aircraft) {
        var even: Cpr1? = null
        var odd: Cpr1? = null
    }

    private val planes = HashMap<Int, State>()
    private val seenAddresses = HashMap<Int, Long>()     // addresses heard in DF11/17/18, for DF4/5/20/21

    /** Your location, for decoding single positions and for distances. */
    @Volatile
    var receiver: Pair<Double, Double>? = null

    var good = 0L
        private set

    /** Feeds one candidate message from the demodulator; true if it was a good one. */
    fun feed(msg: ByteArray, signalDbfs: Double): Boolean {
        val now = clock()
        val decoded = ModeSDecoder.decode(msg) { icao -> (seenAddresses[icao] ?: 0L) > now - ttlMs } ?: return false
        good++
        val df = ModeSDecoder.df(msg)
        if (df == 11 || df == 17 || df == 18) seenAddresses[decoded.icao] = now
        if (df == 11 && planes[decoded.icao] == null) return true     // an all-call alone isn't an aircraft yet
        val st = planes.getOrPut(decoded.icao) { State(Aircraft(decoded.icao)) }
        var a = st.a.copy(messages = st.a.messages + 1, lastSeenMs = now,
            signalDbfs = if (st.a.messages == 0) signalDbfs else st.a.signalDbfs * 0.8 + signalDbfs * 0.2)
        when (decoded) {
            is ModeSMessage.Identification -> a = a.copy(callsign = decoded.callsign, category = decoded.category)
            is ModeSMessage.Squawk -> a = a.copy(squawk = decoded.squawk)
            is ModeSMessage.Altitude -> if (decoded.altitudeFt != null) a = a.copy(altitudeFt = decoded.altitudeFt)
            is ModeSMessage.Velocity -> a = a.copy(
                speedKt = decoded.speedKt ?: a.speedKt, track = decoded.track ?: a.track,
                verticalRateFpm = decoded.verticalRateFpm ?: a.verticalRateFpm, onGround = false)
            is ModeSMessage.Surface -> a = a.copy(onGround = true, speedKt = decoded.groundSpeedKt ?: a.speedKt,
                track = decoded.track ?: a.track, altitudeFt = null)
            is ModeSMessage.Position -> {
                if (decoded.altitudeFt != null && !decoded.gnss) a = a.copy(altitudeFt = decoded.altitudeFt)
                a = position(st, a, decoded, now)
            }
            is ModeSMessage.Seen -> {}
        }
        st.a = a
        return true
    }

    private fun position(st: State, a: Aircraft, p: ModeSMessage.Position, now: Long): Aircraft {
        val c = Cpr1(p.latCpr, p.lonCpr, now)
        if (p.odd) st.odd = c else st.even = c
        val e = st.even
        val o = st.odd
        var pos: Pair<Double, Double>? = null
        // A recent fix of this aircraft is the best reference; else a fresh even/odd pair; else the receiver.
        if (a.lat != null && a.lon != null && now - a.lastPositionMs < 30_000) {
            val l = Cpr.local(p.latCpr, p.lonCpr, p.odd, a.lat, a.lon)
            if (Geo.distanceKm(a.lat, a.lon, l.first, l.second) < 20) pos = l
        }
        if (pos == null && e != null && o != null && abs(e.t - o.t) <= 10_000) {
            pos = Cpr.global(e.lat, e.lon, o.lat, o.lon, oddNewer = p.odd)
            // A global fix far from the last one is more likely a bad message than a jump.
            if (pos != null && a.lat != null && a.lon != null && now - a.lastPositionMs < 60_000 &&
                Geo.distanceKm(a.lat, a.lon, pos.first, pos.second) > 50) pos = null
        }
        val r = receiver
        if (pos == null && r != null && a.lat == null) {
            val l = Cpr.local(p.latCpr, p.lonCpr, p.odd, r.first, r.second)
            // Only when an even/odd pair agrees can a single message be trusted from the receiver alone.
            if (Geo.distanceKm(r.first, r.second, l.first, l.second) < 300 && e != null && o != null) pos = l
        }
        return if (pos == null) a else a.copy(lat = pos.first, lon = pos.second, lastPositionMs = now)
    }

    /** The aircraft heard within the TTL, newest first; forgets older ones. */
    fun snapshot(): List<Aircraft> {
        val now = clock()
        planes.entries.removeAll { now - it.value.a.lastSeenMs > ttlMs }
        seenAddresses.entries.removeAll { now - it.value > ttlMs }
        return planes.values.map { it.a }.sortedByDescending { it.lastSeenMs }
    }

    fun clear() {
        planes.clear()
        seenAddresses.clear()
    }
}
