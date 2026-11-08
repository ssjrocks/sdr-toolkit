"""HF weather fax (WEFAX / radiofax).

On USB with the dial 1.9 kHz below the station's frequency, the picture is
an audio tone from 1500 Hz (black) to 2300 Hz (white). A chart starts with
the start tone (the picture signal switching black/white 300 times a second
for IOC 576, 675 for IOC 288), then about 30 s of phasing lines (a short
white pulse marks where each line begins), then the picture at 120 lines a
minute (60, 90 and 240 are also used), and ends with a 450 Hz stop tone.
Each line is IOC x pi pixels wide.
"""

import math

import numpy as np

from .sstv.decoder import FMDemod

START_HZ = {576: 300.0, 288: 675.0}
STOP_HZ = 450.0


def tone_strength(v: np.ndarray, f: float, rate: int) -> float:
    """Share of the picture signal's variation that is at *f* Hz (0..1)."""
    if len(v) < 16:
        return 0.0
    v = v - v.mean()
    total = float(np.sum(v * v)) + 1e-12
    n = np.arange(len(v))
    c = np.sum(v * np.exp(-2j * np.pi * f * n / rate))
    return float(2 * abs(c) ** 2 / len(v) / total)


class WefaxDecoder:
    """Feed audio; returns events ("start"/"rows"/"done", pixels)."""

    def __init__(self, rate, lpm=120, ioc=576, center=1900.0, deviation=400.0,
                 auto_start=True, auto_stop=True, ppm=0.0, invert=False, max_lines=2400):
        self.rate = rate
        self.demod = FMDemod(rate)
        self.lpm, self.ioc = lpm, ioc
        self.center, self.deviation = center, deviation
        self.auto_start, self.auto_stop = auto_start, auto_stop
        self.ppm, self.invert, self.max_lines = ppm, invert, max_lines
        self.width = int(round(ioc * math.pi))
        self._v = np.zeros(0, np.float32)        # picture signal 0 (black) .. 1 (white)
        self._base = 0                           # sample index of _v[0]
        self.state = "idle"                      # idle, phasing, image
        self._tone_run = 0.0
        self._line_start = 0.0                   # absolute sample of the next line
        self.rows: list[np.ndarray] = []
        self._phasing_rows = False
        self._profile = None
        self.shift = 0.0                         # move the picture right by this fraction of a line
        self._phasing_from = 0

    @property
    def line_samples(self) -> float:
        return self.rate * 60.0 / self.lpm * (1 + self.ppm * 1e-6)

    def start_now(self):
        """Start a picture without waiting for the start tone."""
        self._begin(self._base + len(self._v), phasing=False)

    def feed(self, audio) -> list:
        f, _ = self.demod.process(audio)
        black = self.center - self.deviation
        v = np.clip((f - black) / (2 * self.deviation), 0, 1).astype(np.float32)
        if self.invert:
            v = 1 - v
        self._v = np.concatenate([self._v, v])
        events = []
        half = self.rate // 2
        end = self._base + len(self._v)
        if self.state == "idle":
            if self.auto_start and len(self._v) >= half:
                s = tone_strength(self._v[-half:], START_HZ.get(self.ioc, 300.0), self.rate)
                self._tone_run = self._tone_run + len(v) / self.rate if s > 0.4 else 0.0
                if self._tone_run >= 2.0:
                    self._begin(end, phasing=True)
                    events.append(("start", None))
        elif self.state == "phasing":
            # Fold the phasing lines over the line period to find the pulse.
            if end - self._phasing_from >= 10 * self.line_samples:
                self._find_phase(self._phasing_from, end)
                self.state = "image"
                events.append(("phased", None))
            elif tone_strength(self._v[-half:], START_HZ.get(self.ioc, 300.0), self.rate) > 0.4:
                self._phasing_from = end            # still the start tone
        if self.state == "image":
            events += self._lines()
            if self.auto_stop and len(self._v) >= half and \
                    tone_strength(self._v[-half:], STOP_HZ, self.rate) > 0.4:
                self._tone_run += len(v) / self.rate
                if self._tone_run >= 2.0:
                    # Drop the lines drawn from the stop tone itself.
                    drop = int((self._tone_run + 0.5) * self.lpm / 60) + 1
                    del self.rows[max(len(self.rows) - drop, 0) :]
                    events.append(self.finish())
            else:
                self._tone_run = 0.0
        self._trim()
        return events

    def _begin(self, at, phasing):
        self.rows = []
        self._phasing_rows = phasing
        self._tone_run = 0.0
        self._phasing_from = at
        self._line_start = float(at)
        self.state = "phasing" if phasing else "image"

    def _find_phase(self, a, b):
        L = self.line_samples
        n = int((b - a) // L)
        width = int(L)
        acc = np.zeros(width)
        for k in range(n):
            s = int(a - self._base + k * L)
            seg = self._v[s : s + width]
            if len(seg) == width:
                acc += seg
        acc /= max(n, 1)
        # The pulse is about 5% of the line; find the 5% window that differs
        # most from the rest of the line (brighter or darker).
        w = max(int(0.05 * width), 1)
        c = np.concatenate([[0.0], np.cumsum(np.concatenate([acc, acc[:w]]))])
        means = (c[w : w + width] - c[:width]) / w
        dev = np.abs(means - np.median(acc))
        start = int(np.argmax(dev))
        # Lines begin where the pulse begins.
        self._line_start = a + start + self.shift * L
        while self._line_start + L <= b:
            self._line_start += L
        prof = np.roll(acc, -start)
        idx = (np.arange(self.width) * width / self.width).astype(int)
        self._profile = prof[idx] - prof[idx].mean()

    def _lines(self):
        events = []
        L = self.line_samples
        while self._line_start + L <= self._base + len(self._v):
            s = self._line_start - self._base
            edges = s + np.arange(self.width + 1) * (L / self.width)
            c = np.concatenate([[0.0], np.cumsum(self._v, dtype=np.float64)])
            csum = np.interp(edges, np.arange(len(c)), c)
            row = np.diff(csum) / np.maximum(np.diff(edges), 1e-9)
            row = (np.clip(row, 0, 1) * 255).astype(np.uint8)
            self._line_start += L
            if self._phasing_rows and self._is_phasing(row):
                continue
            self._phasing_rows = False
            self.rows.append(row)
            if len(self.rows) >= self.max_lines:
                events.append(self.finish())
                return events
        if self.state == "image" and self.rows:
            events.append(("rows", None))
        return events

    def _is_phasing(self, row):
        """Does this line look like the phasing lines measured before it?"""
        if self._profile is None:
            return False
        r = row.astype(float) / 255.0
        r -= r.mean()
        den = np.sqrt(np.sum(r * r) * np.sum(self._profile**2)) + 1e-9
        return float(np.sum(r * self._profile)) / den > 0.6

    def finish(self):
        self.state = "idle"
        self._tone_run = 0.0
        return ("done", None)

    def picture(self) -> np.ndarray:
        if not self.rows:
            return np.zeros((1, self.width, 3), np.uint8)
        g = np.stack(self.rows)
        return np.repeat(g[:, :, None], 3, axis=2)

    def _trim(self):
        keep_from = self._base + len(self._v) - 4 * self.rate
        if self.state == "phasing":
            keep_from = min(keep_from, self._phasing_from)
        elif self.state == "image":
            keep_from = min(keep_from, int(self._line_start) - 10)
        cut = int(keep_from - self._base)
        if cut > self.rate:
            self._v = self._v[cut:]
            self._base += cut


def to_audio(img: np.ndarray, rate, lpm=120, ioc=576, start_s=5.0, phasing_s=30.0, stop_s=5.0,
             center=1900.0, deviation=400.0, pulse_at=0.0):
    """Fax audio for a greyscale picture (rows x width, 0..255), for tests."""
    L = rate * 60.0 / lpm
    black, white = center - deviation, center + deviation
    parts = []
    t = np.arange(int(start_s * rate)) / rate
    sq = (np.sin(2 * np.pi * START_HZ[ioc] * t) > 0)
    parts.append(np.where(sq, white, black))
    n_ph = int(phasing_s * lpm / 60)
    for _ in range(n_ph):
        x = (np.arange(int(L)) / L - pulse_at) % 1.0
        parts.append(np.where(x < 0.05, white, black))
    h, w = img.shape
    for r in range(h):
        idx = np.minimum((np.arange(int(L)) / L * w).astype(int), w - 1)
        parts.append(black + img[r][idx] / 255.0 * (white - black))
    t = np.arange(int(stop_s * rate)) / rate
    parts.append(np.where(np.sin(2 * np.pi * STOP_HZ * t) > 0, white, black))
    f = np.concatenate(parts)
    return (0.5 * np.sin(2 * np.pi * np.cumsum(f) / rate)).astype(np.float32)
