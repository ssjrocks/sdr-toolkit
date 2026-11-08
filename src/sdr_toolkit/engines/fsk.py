"""Two-tone FSK demodulation, shared by RTTY and NAVTEX.

Each tone is mixed to 0 Hz and low-pass filtered; the soft bit is
(|mark| - |space|) / (|mark| + |space|), between -1 (space) and +1 (mark),
which doesn't depend on the audio level and copes with one tone fading
more than the other (selective fading on HF).
"""

import numpy as np
from scipy.signal import butter, sosfilt


class FSKDemod:
    def __init__(self, rate: int, mark: float, space: float, baud: float):
        self.rate = rate
        self.mark, self.space, self.baud = float(mark), float(space), float(baud)
        # Filter about the bit rate wide: passes the keying, rejects the other tone.
        cutoff = min(max(baud * 0.75, 20.0), abs(mark - space) * 0.45 + 10)
        self._sos = butter(3, cutoff, fs=rate, output="sos")
        self._zi = np.zeros((2, self._sos.shape[0], 2, 2))
        self._n = 0
        # Power of the whole voice band, to judge how much of it is the two tones.
        self._wide = butter(4, [300, min(3400, rate / 2 - 100)], btype="band", fs=rate, output="sos")
        self._wide_zi = np.zeros((self._wide.shape[0], 2))
        self._pw = butter(2, cutoff, fs=rate, output="sos")
        self._pw_zi = np.zeros((self._pw.shape[0], 2))

    def process(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return (soft bits -1..1, share of the audio in the two tones 0..1) per sample."""
        x = np.asarray(x, dtype=np.float64)
        t = (self._n + np.arange(len(x))) / self.rate
        self._n += len(x)
        mags = []
        for i, f in enumerate((self.mark, self.space)):
            bb = np.stack([x * np.cos(2 * np.pi * f * t), -x * np.sin(2 * np.pi * f * t)])
            bb, self._zi[i] = sosfilt(self._sos, bb, axis=1, zi=self._zi[i])
            mags.append(np.hypot(bb[0], bb[1]))
        m, s = mags
        total = m + s + 1e-9
        wide, self._wide_zi = sosfilt(self._wide, x, zi=self._wide_zi)
        wide_p, self._pw_zi = sosfilt(self._pw, wide * wide, zi=self._pw_zi)
        # A tone of amplitude A filters to A/2: 2 x its power is A^2/2, the tone's power.
        share = np.clip(2 * (m * m + s * s) / np.maximum(wide_p, 1e-12), 0, 1)
        return (m - s) / total, share
