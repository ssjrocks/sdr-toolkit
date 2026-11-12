package com.ssjrocks.sdrtoolkit.engine

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import com.ssjrocks.sdrtoolkit.dsp.AUDIO_RATE

/**
 * 48 kHz mono float audio. Writes never block the DSP thread: the dongle's clock and the phone's
 * never quite agree, so when too much is queued a block is dropped rather than falling behind.
 */
class AudioOut {
    private val track: AudioTrack
    private var written = 0L
    private val maxQueued = AUDIO_RATE / 3            // frames (~330 ms)

    init {
        val min = AudioTrack.getMinBufferSize(AUDIO_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_FLOAT)
        track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build())
            .setAudioFormat(AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_FLOAT)
                .setSampleRate(AUDIO_RATE)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .build())
            .setBufferSizeInBytes(maxOf(min * 4, AUDIO_RATE * 4 / 2))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        track.play()
    }

    fun write(buf: FloatArray, n: Int, volume: Float) {
        if (n <= 0) return
        val queued = written - (track.playbackHeadPosition.toLong() and 0xFFFFFFFFL)
        if (queued > maxQueued) return
        for (i in 0 until n) buf[i] = (buf[i] * volume).coerceIn(-1f, 1f)
        val w = track.write(buf, 0, n, AudioTrack.WRITE_NON_BLOCKING)
        if (w > 0) written += w
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
