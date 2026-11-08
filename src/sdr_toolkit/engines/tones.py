"""Signalling tones on an FM channel: DTMF digits, CTCSS and DCS.

DTMF: Goertzel filters on the 8 tones over 25.6 ms blocks; a digit needs one
low and one high tone well above the others for two blocks.

CTCSS: the 60-260 Hz part of the audio, in 2 s FFTs; a steady peak within
1.5 Hz of a standard tone.

DCS: the sub-audible signal sliced at 134.4 bit/s. A DCS code is a 23-bit
Golay word repeated continuously: 9 bits of code (3 octal digits), the bits
100, and 11 check bits, computed as in the Talkpod firmware's golay()
(drivers/bk4819.c). Every 23-bit window of received bits is checked, the
right way up and inverted.
"""

import numpy as np
from scipy.signal import butter, sosfilt

DTMF_LOW = (697, 770, 852, 941)
DTMF_HIGH = (1209, 1336, 1477, 1633)
DTMF_KEYS = ("123A", "456B", "789C", "*0#D")

CTCSS = (67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5,
         107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 150.0, 151.4, 156.7,
         159.8, 162.2, 165.5, 167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6,
         199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1)

DCS_CODES = (
    0o023, 0o025, 0o026, 0o031, 0o032, 0o036, 0o043, 0o047, 0o051, 0o053, 0o054, 0o065, 0o071,
    0o072, 0o073, 0o074, 0o114, 0o115, 0o116, 0o122, 0o125, 0o131, 0o132, 0o134, 0o143, 0o145,
    0o152, 0o155, 0o156, 0o162, 0o165, 0o172, 0o174, 0o205, 0o212, 0o223, 0o225, 0o226, 0o243,
    0o244, 0o245, 0o246, 0o251, 0o252, 0o255, 0o261, 0o263, 0o265, 0o266, 0o271, 0o274, 0o306,
    0o311, 0o315, 0o325, 0o331, 0o332, 0o343, 0o346, 0o351, 0o356, 0o364, 0o365, 0o371, 0o411,
    0o412, 0o413, 0o423, 0o431, 0o432, 0o445, 0o446, 0o452, 0o454, 0o455, 0o462, 0o464, 0o465,
    0o466, 0o503, 0o506, 0o516, 0o523, 0o526, 0o532, 0o546, 0o565, 0o606, 0o612, 0o624, 0o627,
    0o631, 0o632, 0o654, 0o662, 0o664, 0o703, 0o712, 0o723, 0o731, 0o732, 0o734, 0o743, 0o754,
)
DCS_BAUD = 134.4


def golay(code: int) -> int:
    """The DCS codeword for a 9-bit code, as the Talkpod firmware builds it."""
    cw = 0x800 | code
    w = cw
    for _ in range(12):
        w <<= 1
        if w & 0x1000:
            w ^= 0x08EA
    return cw | (w & 0x0FFE) << 11


def dcs_bits(code: int, invert=False) -> list[int]:
    """One codeword's bits in sending order (bit 0 first)."""
    w = golay(code)
    if invert:
        w ^= 0x7FFFFF
    return [(w >> i) & 1 for i in range(23)]


def goertzel(x, f, rate):
    w = 2 * np.pi * f / rate
    n = np.arange(len(x))
    return abs(np.sum(x * np.exp(-1j * w * n))) ** 2


class ToneDecoder:
    """Feed 8 kHz audio; returns ("dtmf", digit) / ("ctcss", Hz or None) / ("dcs", text or None)."""

    def __init__(self, rate=8000, dtmf=True, ctcss=True, dcs=True, dtmf_min_ms=40):
        self.rate = rate
        self.dtmf_on, self.ctcss_on, self.dcs_on = dtmf, ctcss, dcs
        self._block = int(rate * 0.0256)
        self._need = max(1, int(round(dtmf_min_ms / 25.6)))
        self._dtmf_buf = np.zeros(0)
        self._cand, self._count, self._held = None, 0, None
        self._gap = 0
        self._sub_sos = butter(6, 300, fs=rate, output="sos")
        self._sub_zi = np.zeros((self._sub_sos.shape[0], 2))
        self._sub = np.zeros(0)
        self._since_fft = 0
        self.ctcss_now, self._ctcss_cand = None, None
        # DCS bit slicer
        self._spb = rate / DCS_BAUD
        self._t = 0.0
        self._prev = 0.0
        self._bits: list[int] = []
        self._dcs_hits: list[tuple[int, str]] = []    # (bit index, code text)
        self.dcs_now = None

    def feed(self, audio):
        x = np.asarray(audio, dtype=np.float64)
        events = []
        if self.dtmf_on:
            events += self._dtmf(x)
        sub, self._sub_zi = sosfilt(self._sub_sos, x, zi=self._sub_zi)
        self._sub = np.concatenate([self._sub, sub])
        if self.ctcss_on:
            events += self._ctcss(len(x))
        if self.dcs_on:
            events += self._dcs()
        keep = int(2.5 * self.rate)
        if len(self._sub) > keep and self._t > len(self._sub) - keep + self._spb:
            cut = len(self._sub) - keep
            self._sub = self._sub[cut:]
            self._t -= cut
        return events

    # ---- DTMF ----------------------------------------------------------

    def _dtmf(self, x):
        events = []
        self._dtmf_buf = np.concatenate([self._dtmf_buf, x])
        while len(self._dtmf_buf) >= self._block:
            blk = self._dtmf_buf[: self._block]
            self._dtmf_buf = self._dtmf_buf[self._block :]
            key = self._dtmf_block(blk)
            if key is not None and key == self._cand:
                self._count += 1
            else:
                self._cand, self._count = key, 1 if key is not None else 0
            if key is None:
                self._gap += 1
                if self._gap >= 2:
                    self._held = None
            else:
                self._gap = 0
            if self._cand is not None and self._count >= self._need and self._held != self._cand:
                self._held = self._cand
                events.append(("dtmf", self._cand))
        return events

    def _dtmf_block(self, blk):
        energy = float(np.sum(blk * blk)) + 1e-12
        lo = [goertzel(blk, f, self.rate) for f in DTMF_LOW]
        hi = [goertzel(blk, f, self.rate) for f in DTMF_HIGH]
        i, j = int(np.argmax(lo)), int(np.argmax(hi))
        lo_s, hi_s = sorted(lo), sorted(hi)
        if lo_s[-1] < 6 * lo_s[-2] or hi_s[-1] < 6 * hi_s[-2]:
            return None
        # A pure tone's Goertzel power is (A N / 2)^2 and its energy A^2 N / 2:
        # the two tones should hold most of the block's energy.
        share = 2 * (lo[i] + hi[j]) / (len(blk) * energy)
        twist = lo[i] / (hi[j] + 1e-12)
        if share < 0.6 or not (1 / 8 < twist < 8):
            return None
        return DTMF_KEYS[i][j]

    # ---- CTCSS ---------------------------------------------------------

    def _ctcss(self, n):
        self._since_fft += n
        if self._since_fft < self.rate // 2 or len(self._sub) < 2 * self.rate:
            return []
        self._since_fft = 0
        x = self._sub[-2 * self.rate :]
        x = x - x.mean()
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x)), 1 << 16)) ** 2
        freqs = np.fft.rfftfreq(1 << 16, 1 / self.rate)
        band = (freqs > 60) & (freqs < 260)
        k = int(np.argmax(spec[band]))
        peak_f = float(freqs[band][k])
        # Most of the sub-audible energy should sit in the peak.
        near = band & (np.abs(freqs - peak_f) < 2.0)
        share = float(spec[near].sum() / (spec[band].sum() + 1e-12))
        tone = None
        if share > 0.5 and float(np.mean(x * x)) > 1e-7:
            nearest = min(CTCSS, key=lambda t: abs(t - peak_f))
            if abs(nearest - peak_f) < 1.5:
                tone = nearest
        events = []
        if tone == self._ctcss_cand and tone != self.ctcss_now:
            self.ctcss_now = tone
            events.append(("ctcss", tone))
        self._ctcss_cand = tone
        return events

    # ---- DCS -----------------------------------------------------------

    def _dcs(self):
        spb = self._spb
        sub = self._sub
        while self._t + 2 * spb < len(sub):
            a = self._t
            v = float(np.mean(sub[int(a + 0.3 * spb) : int(a + 0.7 * spb)]))
            if (v > 0) != (self._prev > 0):
                lo, hi = int(a - spb / 2), int(a + spb / 2)
                seg = sub[max(lo, 0) : hi]
                cross = np.flatnonzero(np.diff(np.sign(seg)) != 0)
                if len(cross):
                    pos = max(lo, 0) + cross[np.argmin(np.abs(max(lo, 0) + cross - a))]
                    self._t += 0.2 * (pos - a)
            self._prev = v
            self._bits.append(1 if v > 0 else 0)
            self._t += spb
            self._check_word()
        if len(self._bits) > 400:
            del self._bits[:-200]
            self._dcs_hits = [(i - (len(self._bits) + 200 - len(self._bits)), c) for i, c in self._dcs_hits]
        return self._dcs_report()

    def _check_word(self):
        if len(self._bits) < 23:
            return
        bits = self._bits[-23:]
        word = sum(b << i for i, b in enumerate(bits))
        for inv in (False, True):
            w = word ^ 0x7FFFFF if inv else word
            if (w >> 9) & 7 == 4 and golay(w & 0x1FF) == w:
                code = w & 0x1FF
                text = f"D{code:03o}{'I' if inv else 'N'}"
                self._dcs_hits.append((len(self._bits), text))

    def _dcs_report(self):
        n = len(self._bits)
        self._dcs_hits = [(i, c) for i, c in self._dcs_hits if n - i < 3 * 23]
        recent = [c for _, c in self._dcs_hits]
        counts = {c: recent.count(c) for c in set(recent)}
        seen = [c for c, k in counts.items() if k >= 2]
        events = []
        if not seen:
            if not recent and self.dcs_now is not None:
                self.dcs_now = None
                events.append(("dcs", None))
            return events
        # One bit stream reads as several codes (023N is the same signal as
        # 047I): name a standard, non-inverted one and list the others.
        if self.dcs_now is not None and self.dcs_now.split()[0] in counts:
            return events
        seen.sort(key=lambda c: (int(c[1:4], 8) in DCS_CODES, c.endswith("N"), counts[c]), reverse=True)
        text = seen[0] + (f" (same signal as {', '.join(sorted(seen[1:]))})" if len(seen) > 1 else "")
        self.dcs_now = text
        events.append(("dcs", text))
        return events
