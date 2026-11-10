"""Decoding the receiver's audio with the toolkit's audio decoders (and DSD-FME).

A ``DecoderHost`` runs one decoder mode (Morse, RTTY, PSK, NAVTEX, FT8/FT4, SSTV, weather
fax, APRS, pagers, tones, or DMR/P25 through DSD-FME) on the receiver's 48 kHz audio,
resampled to the rate the decoder wants. It runs on the receiver's thread; the window
collects its events (and recent audio, for the decoder's own waterfall).
"""

import collections
import logging
import threading

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from .dsp import AUDIO_RATE

log = logging.getLogger(__name__)

# decoder mode id: (receiver demodulator, bandwidth Hz) it wants
DECODERS = {
    "morse": ("CW", 500),
    "rtty": ("USB", 3000),
    "psk": ("USB", 3000),
    "navtex": ("USB", 3000),
    "ft8": ("USB", 3000),
    "ft4": ("USB", 3000),
    "sstv": ("USB", 3000),
    "wefax": ("USB", 3000),
    "aprs": ("NFM", 12500),
    "pager": ("NFM", 12500),
    "tones": ("NFM", 12500),
    "dmr": ("NFM", 12500),
}
TAIL = 4096 * 2                 # recent decoder-rate audio kept for its waterfall


class Resampler:
    """48 kHz to another audio rate, keeping state between blocks: a low-pass below the new
    Nyquist, then linear interpolation at a running position (fine for audio decoders, whose
    signals sit well below the cut-off)."""

    def __init__(self, rate_in, rate_out):
        self.rate_in, self.rate_out = int(rate_in), int(rate_out)
        self.step = self.rate_in / self.rate_out
        self.sos = butter(8, 0.45 * self.rate_out, fs=self.rate_in, output="sos") if rate_out < rate_in else None
        self.zi = None
        self.pos = 0.0               # where the next output sample falls, counted from this block's start
        self.last = 0.0              # the previous block's last sample, for interpolating across the edge

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float64)
        if self.rate_in == self.rate_out or not len(x):
            return x.astype(np.float32)
        if self.sos is not None:
            if self.zi is None:
                self.zi = sosfilt_zi(self.sos) * x[0]
            x, self.zi = sosfilt(self.sos, x, zi=self.zi)
        ext = np.concatenate([[self.last], x])            # ext[k + 1] is x[k]; ext[0] is x[-1] before
        idx = np.arange(self.pos, len(x) - 1, self.step)  # positions in x (from just below 0, before the last)
        i = np.floor(idx).astype(int)
        frac = idx - i
        out = ext[i + 1] * (1 - frac) + ext[np.minimum(i + 2, len(ext) - 1)] * frac
        self.pos = (idx[-1] + self.step - len(x)) if len(idx) else self.pos - len(x)
        self.last = x[-1]
        return out.astype(np.float32)


class DecoderHost:
    def __init__(self, mode, values):
        self.mode = mode
        self.id = mode.id
        self.values = dict(values)
        self.events: collections.deque = collections.deque(maxlen=2000)
        self.tail = np.zeros(0, np.float32)
        self.error = ""
        self._lock = threading.Lock()
        self._todo: collections.deque = collections.deque()
        self.mode.start(self.values)                      # may raise (missing parts, settings)
        self.rate = int(self.mode.rate)
        self.resample = Resampler(AUDIO_RATE, self.rate)

    def call(self, fn):
        """Run fn(mode) on the receiver's thread (set_frequency, apply...)."""
        self._todo.append(fn)

    def feed(self, audio48: np.ndarray, now: float):
        while self._todo:
            fn = self._todo.popleft()
            try:
                fn(self.mode)
            except Exception:  # a bad setting must not stop the receiver
                log.exception("decoder %s", self.id)
        x = self.resample(audio48)
        if not len(x):
            return                                       # a short block can give no samples at a lower rate
        try:
            events = list(self.mode.feed(x, now)) + list(self.mode.poll())
        except Exception as e:  # shown, and the decoder is left stopped
            log.exception("decoder %s", self.id)
            self.error = f"{self.mode.name} stopped: {e}"
            events = []
        with self._lock:
            self.events.extend(events)
            self.tail = np.concatenate([self.tail, x])[-TAIL:]

    def poll_idle(self):
        """Decoders that run outside programs produce output even when no audio comes."""
        try:
            evs = list(self.mode.poll())
        except Exception:
            evs = []
        with self._lock:
            self.events.extend(evs)

    def drain(self):
        with self._lock:
            out = list(self.events)
            self.events.clear()
            tail = self.tail.copy()
        return out, tail

    def stop(self):
        try:
            return list(self.mode.stop())
        except Exception:
            log.exception("stopping decoder %s", self.id)
            return []
