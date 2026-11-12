package com.ssjrocks.sdrtoolkit.engine

import android.content.Context
import com.ssjrocks.sdrtoolkit.dsp.DemodMode
import org.json.JSONArray
import org.json.JSONObject

/** Everything the receiver remembers between runs. */
data class RxSettings(
    val tunedHz: Long = 101_100_000,
    val centerHz: Long = 100_500_000,
    val mode: DemodMode = DemodMode.WFM,
    val bandwidth: Int = DemodMode.WFM.defaultBw,
    val stepHz: Int = 100_000,
    val squelchOn: Boolean = false,
    val squelchDb: Int = -60,
    val volume: Float = 0.7f,
    val muted: Boolean = false,
    val sampleRate: Int = 2_400_000,
    val gainAuto: Boolean = true,
    val gainTenths: Int = 297,
    val rtlAgc: Boolean = false,
    val ppm: Int = 0,
    val directSampling: Int = 0,
    val offsetTuning: Boolean = false,
    val biasTee: Boolean = false,
    val deemphasisUs: Int = 50,
    val fftSize: Int = 4096,
    val averaging: Float = 0.5f,
    val peakHold: Boolean = false,
    val waterfallRange: Int = 60,
    val adsbTtlS: Int = 60,
    val miles: Boolean = false,
    val myLat: Double? = null,
    val myLon: Double? = null,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("tunedHz", tunedHz).put("centerHz", centerHz).put("mode", mode.name).put("bandwidth", bandwidth)
        .put("stepHz", stepHz).put("squelchOn", squelchOn).put("squelchDb", squelchDb).put("volume", volume.toDouble())
        .put("muted", muted).put("sampleRate", sampleRate).put("gainAuto", gainAuto).put("gainTenths", gainTenths)
        .put("rtlAgc", rtlAgc).put("ppm", ppm).put("directSampling", directSampling).put("offsetTuning", offsetTuning)
        .put("biasTee", biasTee).put("deemphasisUs", deemphasisUs).put("fftSize", fftSize)
        .put("averaging", averaging.toDouble()).put("peakHold", peakHold).put("waterfallRange", waterfallRange)
        .put("adsbTtlS", adsbTtlS).put("miles", miles)
        .apply {
            if (myLat != null && myLon != null) put("myLat", myLat).put("myLon", myLon)
        }

    companion object {
        fun fromJson(o: JSONObject): RxSettings {
            val d = RxSettings()
            return RxSettings(
                tunedHz = o.optLong("tunedHz", d.tunedHz),
                centerHz = o.optLong("centerHz", d.centerHz),
                mode = runCatching { DemodMode.valueOf(o.optString("mode")) }.getOrDefault(d.mode),
                bandwidth = o.optInt("bandwidth", d.bandwidth),
                stepHz = o.optInt("stepHz", d.stepHz),
                squelchOn = o.optBoolean("squelchOn", d.squelchOn),
                squelchDb = o.optInt("squelchDb", d.squelchDb),
                volume = o.optDouble("volume", d.volume.toDouble()).toFloat(),
                muted = o.optBoolean("muted", d.muted),
                sampleRate = o.optInt("sampleRate", d.sampleRate),
                gainAuto = o.optBoolean("gainAuto", d.gainAuto),
                gainTenths = o.optInt("gainTenths", d.gainTenths),
                rtlAgc = o.optBoolean("rtlAgc", d.rtlAgc),
                ppm = o.optInt("ppm", d.ppm),
                directSampling = o.optInt("directSampling", d.directSampling),
                offsetTuning = o.optBoolean("offsetTuning", d.offsetTuning),
                biasTee = o.optBoolean("biasTee", d.biasTee),
                deemphasisUs = o.optInt("deemphasisUs", d.deemphasisUs),
                fftSize = o.optInt("fftSize", d.fftSize),
                averaging = o.optDouble("averaging", d.averaging.toDouble()).toFloat(),
                peakHold = o.optBoolean("peakHold", d.peakHold),
                waterfallRange = o.optInt("waterfallRange", d.waterfallRange),
                adsbTtlS = o.optInt("adsbTtlS", d.adsbTtlS),
                miles = o.optBoolean("miles", d.miles),
                myLat = if (o.has("myLat")) o.optDouble("myLat") else null,
                myLon = if (o.has("myLon")) o.optDouble("myLon") else null,
            )
        }
    }
}

/** A relay or rtl_tcp server the user added or connected to. */
data class SavedSource(val name: String, val host: String, val port: Int, val password: String = "") {
    val key get() = "$host:$port"

    fun toJson(): JSONObject = JSONObject().put("name", name).put("host", host).put("port", port).put("password", password)

    companion object {
        fun fromJson(o: JSONObject) = SavedSource(o.optString("name"), o.optString("host"), o.optInt("port"),
            o.optString("password"))
    }
}

/** SharedPreferences, as JSON. Passwords stay on the phone, in the app's private storage. */
class Store(context: Context) {
    private val prefs = context.getSharedPreferences("sdr-toolkit", Context.MODE_PRIVATE)

    fun loadSettings(): RxSettings = prefs.getString("settings", null)
        ?.let { runCatching { RxSettings.fromJson(JSONObject(it)) }.getOrNull() } ?: RxSettings()

    fun saveSettings(s: RxSettings) = prefs.edit().putString("settings", s.toJson().toString()).apply()

    fun loadSources(): List<SavedSource> = prefs.getString("sources", null)?.let { text ->
        runCatching {
            val a = JSONArray(text)
            (0 until a.length()).map { SavedSource.fromJson(a.getJSONObject(it)) }
        }.getOrNull()
    } ?: emptyList()

    fun saveSources(list: List<SavedSource>) {
        val a = JSONArray()
        list.forEach { a.put(it.toJson()) }
        prefs.edit().putString("sources", a.toString()).apply()
    }

    /** Remembered passwords for relays found by scanning or in the directory, by host:port. */
    fun password(key: String): String = prefs.getString("pw:$key", "") ?: ""

    fun setPassword(key: String, password: String) = prefs.edit().putString("pw:$key", password).apply()
}
