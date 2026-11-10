"""The survey (mega-spectrum): the dongle hops across a wide range and the slices are stitched.

Each step tunes the dongle, throws away the samples of a settle time (the tuner's PLL and
gain settling, and samples from the previous frequency still on their way, which over a
relay can be a few hundred ms), then measures for the dwell time and keeps the middle 80% of
the slice (the edges are on the dongle's filter slopes). The dwell is cut into groups of a few
spectra; each group is averaged (which smooths the noise) and the slice keeps the strongest group,
so a longer dwell catches signals that are only on now and then (pagers, DMR, data bursts). Only the range being looked at is
swept (the window's "Sweep what's shown"), so passes over a smaller range are faster; a range
narrower than one slice isn't swept at all, the dongle just stays there.

``Capture`` is the window's side: every slice measured, kept at the dongle's full detail across
its whole tuning range, so zooming and panning show what's already been captured.
"""

import collections
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
        self.done: collections.deque = collections.deque(maxlen=5000)   # (f_lo, dB) for the window
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
        self.done.append((self.current + (keep - self.fft / 2) * self.rate / self.fft, usable))
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


class Capture:
    """Everything the survey has measured, at full detail (one bin is rate / fft) over the tuner's
    whole range: the latest level of every bin, and the waterfall's history as rows of what each
    interval brought. Drawing any range rebuilds its waterfall from those rows, so zooming in
    shows the detail that was captured, not a stretched picture."""

    def __init__(self, min_hz, max_hz, res, max_rows=400):
        self.min_hz, self.max_hz, self.res = float(min_hz), float(max_hz), float(res)
        self.n = int((self.max_hz - self.min_hz) / self.res) + 1
        self.latest = np.full(self.n, np.nan, np.float32)
        self.base = self.latest.copy()              # the levels before the oldest row
        self.rows: collections.deque = collections.deque()   # oldest first: [(start bin, dB float16), ...]
        self.pending: list = []
        self.max_rows = max_rows

    def index(self, hz) -> int:
        return int(np.clip(round((hz - self.min_hz) / self.res), 0, self.n))

    def add(self, f_lo, db):
        a = int(round((f_lo - self.min_hz) / self.res))
        db = np.asarray(db, np.float32)
        if a < 0:
            db, a = db[-a:], 0
        db = db[: max(self.n - a, 0)]
        if not len(db):
            return
        self.latest[a:a + len(db)] = db
        self.pending.append((a, db.astype(np.float16)))

    def push_row(self):
        """Start a new waterfall row (what was measured since the last one)."""
        self.rows.append(self.pending)
        self.pending = []
        while len(self.rows) > self.max_rows:
            for a, d in self.rows.popleft():
                self.base[a:a + len(d)] = d

    def columns(self, lo, hi, width):
        """The first bin of each of `width` columns across [lo, hi) (repeated when zoomed in past one bin)."""
        i0 = self.index(lo)
        i1 = max(self.index(hi), i0 + 1)
        return np.minimum(np.floor(np.linspace(i0, i1, width, endpoint=False)).astype(np.int64), self.n - 1), i1

    @staticmethod
    def _reduce(levels, starts):
        """The strongest bin of each column (NaN where nothing's been measured)."""
        with np.errstate(invalid="ignore"):
            return np.fmax.reduceat(levels, starts)

    def spectrum(self, lo, hi, width) -> np.ndarray:
        cols, i1 = self.columns(lo, hi, width)
        return self._reduce(self.latest[cols[0]:i1], cols - cols[0])

    def waterfall(self, lo, hi, width) -> "View":
        return Capture.View(self, lo, hi, width)

    class View:
        """One range's waterfall, newest row first, kept up to date row by row."""

        def __init__(self, cap, lo, hi, width):
            self.cap = cap
            self.cols, i1 = cap.columns(lo, hi, width)
            self.i0, self.i1 = int(self.cols[0]), i1
            self.rel = self.cols - self.i0
            self.state = cap.base[self.i0:self.i1].copy()
            self.line = Capture._reduce(self.state, self.rel)
            self.image = np.full((cap.max_rows, width), np.nan, np.float32)
            n = len(cap.rows)
            for k, row in enumerate(cap.rows):            # oldest first, into place (newest at the top)
                self._apply(row)
                self.image[n - 1 - k] = self.line
            self.count = n

        def add_row(self, row):
            self._apply(row)
            self.image = np.roll(self.image, 1, axis=0)
            self.image[0] = self.line
            self.count = min(self.count + 1, len(self.image))

        def _apply(self, row):
            for a, d in row:
                b = a + len(d)
                lo, hi = max(a, self.i0), min(b, self.i1)
                if lo >= hi:
                    continue
                self.state[lo - self.i0:hi - self.i0] = d[lo - a:hi - a]
                # The columns those bins fall in.
                pa = max(int(np.searchsorted(self.cols, lo, "right")) - 1, 0)
                pa = int(np.searchsorted(self.cols, self.cols[pa], "left"))
                pb = int(np.searchsorted(self.cols, hi - 1, "right"))
                end = self.rel[pb] if pb < len(self.rel) else len(self.state)
                self.line[pa:pb] = Capture._reduce(self.state[self.rel[pa]:end], self.rel[pa:pb] - self.rel[pa])
