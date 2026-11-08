"""BPSK31/63/125 with Varicode.

The signal is mixed down from its audio frequency, and each symbol is the
complex sample at the symbol's middle: a phase reversal from the previous
symbol is a 0, no change a 1. Characters are Varicode words separated by
"00". The symbol clock is kept from the amplitude dips at reversals (early /
late comparison), and AFC follows the residual phase rotation.
"""

import numpy as np
from scipy.signal import butter, sosfilt

VARICODE = {
    0: "1010101011", 1: "1011011011", 2: "1011101101", 3: "1101110111", 4: "1011101011",
    5: "1101011111", 6: "1011101111", 7: "1011111101", 8: "1011111111", 9: "11101111",
    10: "11101", 11: "1101101111", 12: "1011011101", 13: "11111", 14: "1101110101",
    15: "1110101011", 16: "1011110111", 17: "1011110101", 18: "1110101101", 19: "1110101111",
    20: "1101011011", 21: "1101101011", 22: "1101101101", 23: "1101010111", 24: "1101111011",
    25: "1101111101", 26: "1110110111", 27: "1101010101", 28: "1101011101", 29: "1110111011",
    30: "1011111011", 31: "1101111111", 32: "1", 33: "111111111", 34: "101011111",
    35: "111110101", 36: "111011011", 37: "1011010101", 38: "1010111011", 39: "101111111",
    40: "11111011", 41: "11110111", 42: "101101111", 43: "111011111", 44: "1110101",
    45: "110101", 46: "1010111", 47: "110101111", 48: "10110111", 49: "10111101",
    50: "11101101", 51: "11111111", 52: "101110111", 53: "101011011", 54: "101101011",
    55: "110101101", 56: "110101011", 57: "110110111", 58: "11110101", 59: "110111101",
    60: "111101101", 61: "1010101", 62: "111010111", 63: "1010101111", 64: "1010111101",
    65: "1111101", 66: "11101011", 67: "10101101", 68: "10110101", 69: "1110111",
    70: "11011011", 71: "11111101", 72: "101010101", 73: "1111111", 74: "111111101",
    75: "101111101", 76: "11010111", 77: "10111011", 78: "11011101", 79: "10101011",
    80: "11010101", 81: "111011101", 82: "10101111", 83: "1101111", 84: "1101101",
    85: "101010111", 86: "110110101", 87: "101011101", 88: "101110101", 89: "101111011",
    90: "1010101101", 91: "111110111", 92: "111101111", 93: "111111011", 94: "1010111111",
    95: "101101101", 96: "1011011111", 97: "1011", 98: "1011111", 99: "101111",
    100: "101101", 101: "11", 102: "111101", 103: "1011011", 104: "101011", 105: "1101",
    106: "111101011", 107: "10111111", 108: "11011", 109: "111011", 110: "1111", 111: "111",
    112: "111111", 113: "110111111", 114: "10101", 115: "10111", 116: "101", 117: "110111",
    118: "1111011", 119: "1101011", 120: "11011111", 121: "1011101", 122: "111010101",
    123: "1010110111", 124: "110111011", 125: "1010110101", 126: "1011010111", 127: "1110110101",
}
DECODE = {v: chr(k) for k, v in VARICODE.items()}


def encode_bits(text: str, preamble=32, postamble=32) -> list[int]:
    """Varicode bits for *text* (for tests): idle reversals, characters, idle."""
    bits = [0] * preamble
    for ch in text:
        bits += [int(b) for b in VARICODE[ord(ch) & 0x7F]] + [0, 0]
    return bits + [0] * postamble


def to_audio(bits, rate, freq, baud=31.25):
    """BPSK audio with raised-cosine amplitude at reversals (for tests)."""
    sps = int(round(rate / baud))
    out = []
    t = np.arange(sps) / sps
    prev = 1.0
    for b in bits:
        cur = prev if b else -prev
        if cur == prev:
            env = np.full(sps, cur)
        else:
            env = prev * np.cos(np.pi * t)       # swings through zero to the new phase
        out.append(env)
        prev = cur
    env = np.concatenate(out)
    n = np.arange(len(env))
    return (0.5 * env * np.cos(2 * np.pi * freq * n / rate)).astype(np.float32)


class PSKDecoder:
    def __init__(self, rate, freq=1000.0, baud=31.25, afc=True, squelch=0.0):
        self.rate, self.freq, self.baud, self.afc = rate, float(freq), baud, afc
        # Characters are dropped while the signal's share (see below) is under this.
        self.squelch = squelch
        self.center = self.freq
        self.afc_range = 15.0
        self.sps = rate / baud
        self._sos = butter(4, baud * 1.2, fs=rate, output="sos")
        self._zi = np.zeros((self._sos.shape[0], 2, 2))
        # Squelch: the signal's share of the power within +-4 symbol rates of it.
        # Noise spreads evenly (about 0.3); a PSK signal sits in the narrow part (near 1).
        self._wsos = butter(4, baud * 4, fs=rate, output="sos")
        self._wzi = np.zeros((self._wsos.shape[0], 2, 2))
        self._pn = np.zeros(0)
        self._pw = np.zeros(0)
        self.share = 0.0
        self._phase = 0.0
        self._z = np.zeros(0, complex)
        self._t = self.sps / 2
        self._last = None
        self._bits = ""
        self.quality = 0.0                       # 0..1, how cleanly symbols sit at 0 / 180 degrees

    def set_freq(self, f):
        self.freq = self.center = float(f)

    def feed(self, audio) -> str:
        x = np.asarray(audio, dtype=np.float64)
        w = 2 * np.pi * self.freq / self.rate
        ph = self._phase + w * np.arange(len(x))
        self._phase = float((self._phase + w * len(x)) % (2 * np.pi))
        bb = np.stack([x * np.cos(ph), -x * np.sin(ph)])
        wide, self._wzi = sosfilt(self._wsos, bb, axis=1, zi=self._wzi)
        bb, self._zi = sosfilt(self._sos, bb, axis=1, zi=self._zi)
        self._z = np.concatenate([self._z, bb[0] + 1j * bb[1]])
        self._pn = np.concatenate([self._pn, bb[0] ** 2 + bb[1] ** 2])
        self._pw = np.concatenate([self._pw, wide[0] ** 2 + wide[1] ** 2])
        out = []
        q = self.sps / 4
        while self._t + q + 2 < len(self._z):
            i = int(self._t)
            z = self._z[i]
            early, late = abs(self._z[int(self._t - q)]), abs(self._z[int(self._t + q)])
            # Move towards the amplitude peak (the middle of the symbol).
            self._t += self.sps + 0.08 * (late - early) / (late + early + 1e-12) * self.sps
            span = slice(max(i - int(4 * self.sps), 0), i + 1)
            self.share = float(np.sum(self._pn[span]) / (np.sum(self._pw[span]) + 1e-12))
            if self._last is not None:
                d = z * np.conj(self._last)
                if abs(d) > 0:
                    ang = np.angle(d)
                    err = np.angle(np.exp(2j * ang)) / 2          # off from 0 or 180 degrees
                    self.quality = 0.9 * self.quality + 0.1 * (1 - abs(err) / (np.pi / 2))
                    if self.afc:
                        # Follow drift, but only within afc_range of where the user put it.
                        f = self.freq + 0.02 * err * self.baud / (2 * np.pi) * 4
                        self.freq = float(np.clip(f, self.center - self.afc_range, self.center + self.afc_range))
                bit = "1" if d.real > 0 else "0"
                ch = self._bit(bit)
                if ch is not None and self.share >= self.squelch:
                    out.append(ch)
            self._last = z
        keep = int(self._t - q) - 2
        if keep > self.rate:
            keep = min(keep, int(self._t - 4 * self.sps) - 2)
            if keep > 0:
                self._z = self._z[keep:]
                self._pn = self._pn[keep:]
                self._pw = self._pw[keep:]
                self._t -= keep
        return "".join(out)

    def _bit(self, bit):
        self._bits += bit
        if self._bits.endswith("00"):
            # Codes start with 1; an odd run of idle zeros leaves one in front.
            code = self._bits[:-2].lstrip("0")
            self._bits = ""
            if code:
                return DECODE.get(code, "")
        elif len(self._bits) > 14:
            self._bits = self._bits[-2:]                       # noise: start again
        return None
