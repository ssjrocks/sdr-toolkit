"""Tone detection: audio in, tone on/off edges out at 1 ms resolution.

The tone is measured as a fraction of the audio between 300 and 3000 Hz:
the power within +-bandwidth/2 of the tone frequency divided by the power of
the whole voice band. A keyed MCW tone on an FM channel gives a fraction near
1 (FM capture makes the tone dominate), receiver noise and voice give a few
percent, and both stay that way at any volume. CTCSS, DCS and the 55 Hz tail
tone are below the band and the 1000 / 850 Hz Roger beep is outside the tone
filter, so none of them key the detector.
"""

import numpy as np
from scipy.signal import bessel, butter, sosfilt

RATE = 8000
STEP = RATE // 1000  # samples per 1 ms output value
_WIDE_BAND = (300.0, 3000.0)


class ToneDetector:
    """Stateful filter bank; feed audio in any chunk size."""

    def __init__(self, freq: float = 700.0, bandwidth: float = 100.0, rate: int = RATE):
        self.rate = rate
        self.step = rate // 1000
        self.freq = freq
        self.bandwidth = bandwidth
        self._phase = 0.0
        self._leftover = np.zeros(0, dtype=np.float64)
        self._wide_sos = butter(4, _WIDE_BAND, btype="band", fs=rate, output="sos")
        self._wide_zi = np.zeros((self._wide_sos.shape[0], 2))
        self._design()

    def _design(self) -> None:
        cutoff = self.bandwidth / 2
        # Complex baseband low-pass: the tone filter is f0 +- cutoff. Bessel,
        # because a filter that rings would make the wide-band power dip
        # below zero as a tone ends, and the ratio spike into a false dit.
        self._lp_sos = bessel(4, cutoff, fs=self.rate, output="sos", norm="mag")
        self._lp_zi = np.zeros((self._lp_sos.shape[0], 2, 2))
        # The wide-band power gets the same smoothing, so the ratio's
        # numerator and denominator have the same delay.
        self._pw_sos = self._lp_sos
        self._pw_zi = np.zeros((self._pw_sos.shape[0], 2))

    def set_tone(self, freq: float, bandwidth: float | None = None) -> None:
        self.freq = float(freq)
        if bandwidth is not None and bandwidth != self.bandwidth:
            self.bandwidth = float(bandwidth)
            self._design()

    def process(self, audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return (tone fraction 0..1, voice-band level in dBFS), one value per ms."""
        x = np.concatenate([self._leftover, np.asarray(audio, dtype=np.float64)])
        usable = len(x) // self.step * self.step
        self._leftover = x[usable:]
        x = x[:usable]
        if not usable:
            return np.zeros(0), np.zeros(0)

        w = 2 * np.pi * self.freq / self.rate
        phase = self._phase + w * np.arange(usable)
        self._phase = float((self._phase + w * usable) % (2 * np.pi))
        bb = np.stack([x * np.cos(phase), -x * np.sin(phase)])
        bb, self._lp_zi = sosfilt(self._lp_sos, bb, axis=1, zi=self._lp_zi)
        # A sine of amplitude A mixes down to A/2; times 2 gives its power A^2/2.
        tone_p = 2.0 * (bb[0] ** 2 + bb[1] ** 2)

        wide, self._wide_zi = sosfilt(self._wide_sos, x, zi=self._wide_zi)
        wide_p, self._pw_zi = sosfilt(self._pw_sos, wide * wide, zi=self._pw_zi)
        wide_p = np.maximum(wide_p, 1e-12)

        tone_p = tone_p[self.step - 1 :: self.step]
        wide_p = wide_p[self.step - 1 :: self.step]
        ratio = np.clip(tone_p / wide_p, 0.0, 1.5)
        level = 10 * np.log10(wide_p)
        return ratio, level


class Keyer:
    """Hysteresis on the tone fraction; yields (tone_on, duration_ms) runs.

    A state change only counts once it has lasted ``min_on_ms`` (to on) or
    ``min_off_ms`` (to off); the run is still timed from the first crossing.
    """

    def __init__(
        self,
        on_threshold: float = 0.6,
        off_threshold: float = 0.33,
        min_level_db: float = -65.0,
        min_on_ms: int = 12,
        min_off_ms: int = 6,
    ):
        self.on_threshold = on_threshold
        self.off_threshold = off_threshold
        self.min_level_db = min_level_db
        self.min_on_ms = min_on_ms
        self.min_off_ms = min_off_ms
        self.state = False
        self._run = 0          # ms in the current confirmed state
        self._pending = 0      # ms the opposite state has lasted so far

    def set_sensitivity(self, on_threshold: float) -> None:
        self.on_threshold = on_threshold
        self.off_threshold = on_threshold * 0.55

    @property
    def current_run_ms(self) -> int:
        """How long the current state has lasted, including any pending change."""
        return self._run + self._pending

    def feed(self, ratio: np.ndarray, level: np.ndarray) -> list[tuple[bool, int]]:
        runs = []
        for r, lv in zip(ratio, level):
            if self.state:
                other = r < self.off_threshold or lv < self.min_level_db
                need = self.min_off_ms
            else:
                other = r > self.on_threshold and lv >= self.min_level_db
                need = self.min_on_ms
            if other:
                self._pending += 1
                if self._pending >= need:
                    runs.append((self.state, self._run))
                    self.state = not self.state
                    self._run = self._pending
                    self._pending = 0
            else:
                # A glitch shorter than the debounce belongs to the current run.
                self._run += self._pending + 1
                self._pending = 0
        return runs
