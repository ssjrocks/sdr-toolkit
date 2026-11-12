package com.ssjrocks.sdrtoolkit.engine

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.Build
import com.ssjrocks.sdrtoolkit.dsp.AUDIO_RATE

/**
 * 48 kHz mono 16-bit audio. Writes never block the DSP thread: whatever doesn't fit in the
 * track's buffer is dropped (the dongle's clock and the phone's never quite agree, and falling
 * behind would add delay).
 *
 * A streaming AudioTrack only starts playing once its buffer has filled to the start threshold,
 * which is the whole buffer unless set lower (Android 12+). So nothing here may stop writing
 * before the buffer is full: an earlier version capped the queue at 1/3 s of a 1/2 s buffer,
 * and never made a sound.
 */
class AudioOut {
    private val track: AudioTrack
    private var pcm = ByteArray(0)

    /** Samples dropped because the buffer was full. */
    var dropped = 0L
        private set

    init {
        val min = AudioTrack.getMinBufferSize(AUDIO_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build())
            .setAudioFormat(AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(AUDIO_RATE)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .build())
            .setBufferSizeInBytes(maxOf(min * 2, AUDIO_RATE * 2 / 4))       // 1/4 s
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            try {
                track.setStartThresholdInFrames(AUDIO_RATE / 20)                // start after 50 ms
            } catch (_: Exception) {
            }
        }
        track.play()
    }

    fun write(buf: FloatArray, n: Int, volume: Float) {
        if (n <= 0) return
        if (pcm.size < n * 2) pcm = ByteArray(n * 2)
        for (i in 0 until n) {
            val v = (buf[i] * volume * 32767f).toInt().coerceIn(-32768, 32767)
            pcm[2 * i] = v.toByte()
            pcm[2 * i + 1] = (v shr 8).toByte()
        }
        val w = track.write(pcm, 0, n * 2, AudioTrack.WRITE_NON_BLOCKING)
        if (w in 0 until n * 2) dropped += (n * 2 - w) / 2
    }

    fun close() {
        try {
            track.pause()
            track.flush()
            track.release()
        } catch (_: IllegalStateException) {
        }
    }
}
