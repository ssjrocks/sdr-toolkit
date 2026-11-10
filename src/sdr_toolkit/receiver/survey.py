"""The survey (mega-spectrum): the dongle hops across a wide range and the slices are stitched.

Each step tunes the dongle, throws away the samples of a settle time (the tuner's PLL and
gain settling, and samples from the previous frequency still on their way, which over a
relay can be a few hundred ms), then measures for the dwell time and keeps the middle 80% of
the slice (the edges are on the dongle's filter slopes). The dwell is cut into groups of a few
spectra; each group is averaged (which smooths the noise) and the slice keeps the strongest group,
so a longer dwell catches signals that are only on now and then (pagers, DMR, data bursts). Only the range being looked at is
swept, so zooming in makes passes faster; a range narrower than one slice isn't swept at
all, the dongle just stays there.
"""

import time

import numpy as np

USABLE = 0.8
# Tuning range (Hz) per tuner type (librtlsdr's tuner numbers).
TUNER_RANGE = {1: (52e6, 2200e6), 2: (22e6, 948e6), 3: (22e6, 1100e6), 4: (146e6, 924e6),
               5: (24e6, 1766e6), 6: (24e6, 1766e6)}


class Survey:
    def __init__(self, rate, tuner=5, settle=0.15, fft=1024, frames=6, dwell=0.0):
        self.rate = int(rate)
        self.min_hz, self.max_hz = TUNER_RANGE.get(tuner, (24e6, 1766e6))
        self.settle = float(settle)
        self.fft = int(fft)
        self.frames = int(frames)                   # spectra averaged per group
        self.dwell = float(dwell)                   # seconds measured per slice (at least one group)
        self.window = np.hanning(self.fft).astype(np.float32)
        self.norm = 20 * np.log10(np.sum(self.window))
        self.segments: dict[float, tuple] = {}     # centre -> (time, f_lo, f_hi, dB of the usable bins)
        self.lo, self.hi = self.min_hz, self.max_hz
        self.centers: list[float] = []
        self.i = 0
        self.current = None
        self.tuned_at = 0.0
        self._acc = None
        self._n = 0
        self._best = None
        self._groups = 0
        self._buf = np.zeros(0, np.complex64)
        self.pass_started = time.monotonic()
        self.last_pass = 0.0
        self.set_view(self.lo, self.hi)

    def set_view(self, lo, hi):
        """Sweep only [lo, hi] (clamped to the tuner's range)."""
        lo, hi = max(lo, self.min_hz), min(hi, self.max_hz)
        if hi <= lo:
            hi = lo + self.rate * USABLE
        self.lo, self.hi = lo, hi
        step = self.rate * USABLE
        if hi - lo <= step:
            self.centers = [(lo + hi) / 2]
        else:
            n = int(np.ceil((hi - lo) / step))
            self.centers = [lo + step * (k + 0.5) for k in range(n)]
        self.i = 0
        self.pass_started = time.monotonic()

    def next_center(self):
        """Where to tune next; call tuned() once the command is sent."""
        return self.centers[self.i % len(self.centers)]

    def tuned(self, center, now):
        self.current = center
        self.tuned_at = now
        self._acc = None
        self._n = 0
        self._best = None
        self._groups = 0
        self._buf = np.zeros(0, np.complex64)

    def groups_wanted(self) -> int:
        return max(1, int(np.ceil(self.dwell * self.rate / (self.fft * self.frames))))

    def feed(self, x, now) -> bool:
        """Samples since the last call. Returns True when this slice is done (tune the next)."""
        if self.current is None or now - self.tuned_at < self.settle:
            return False                            # still settling: these may be the old frequency's
        self._buf = np.concatenate([self._buf, x])
        wanted = self.groups_wanted()
        while self._groups < wanted:
            n = min(len(self._buf) // self.fft, self.frames - self._n)
            if n <= 0:
                break
            seg = self._buf[: n * self.fft].reshape(n, self.fft) * self.window
            self._buf = self._buf[n * self.fft:]
            p = (np.abs(np.fft.fft(seg, axis=1)) ** 2).sum(axis=0)
            self._acc = p if self._acc is None else self._acc + p
            self._n += n
            if self._n == self.frames:
                self._best = self._acc if self._best is None else np.maximum(self._best, self._acc)
                self._acc, self._n = None, 0
                self._groups += 1
        if self._groups < wanted:
            return False
        db = 10 * np.log10(np.fft.fftshift(self._best) / self.frames + 1e-20) - self.norm
        keep = int(self.fft * (1 - USABLE) / 2)
        usable = db[keep:self.fft - keep].astype(np.float32)
        half = self.rate * USABLE / 2
        self.segments[self.current] = (now, self.current - half, self.current + half, usable)
        if len(self.segments) > 4000:                 # forget the oldest, far away
            for k in sorted(self.segments, key=lambda c: self.segments[c][0])[:1000]:
                del self.segments[k]
        self.i += 1
        if self.i % len(self.centers) == 0:
            self.last_pass = time.monotonic() - self.pass_started
            self.pass_started = time.monotonic()
        return True

    def stitched(self, nbins) -> np.ndarray:
        """The spectrum over [lo, hi] in nbins, the strongest of the slices covering each bin (NaN where
        nothing's been measured yet)."""
        out = np.full(nbins, np.nan, np.float32)
        edges = np.linspace(self.lo, self.hi, nbins + 1)
        for _, f_lo, f_hi, db in list(self.segments.values()):     # a copy: the receiver thread adds slices
            if f_hi < self.lo or f_lo > self.hi:
                continue
            freqs = np.linspace(f_lo, f_hi, len(db), endpoint=False)
            idx = np.searchsorted(edges, freqs) - 1
            ok = (idx >= 0) & (idx < nbins)
            if not ok.any():
                continue
            seg = np.full(nbins, -np.inf, np.float32)
            np.maximum.at(seg, idx[ok], db[ok])
            have = np.isfinite(seg)
            out[have] = np.fmax(out[have], seg[have])
        return out
