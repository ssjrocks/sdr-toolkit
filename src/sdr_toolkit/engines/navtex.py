"""NAVTEX: SITOR-B (CCIR 476) at 100 baud, 170 Hz shift.

Every character is a 7-bit word with exactly four ones, so a word's weight
shows whether the decoder is aligned and the right way up. Each character
is sent twice: the "DX" copy, and an "RX" copy five word slots later; DX and
RX slots alternate. When one copy is damaged the other is used.

The code table's source doesn't say which bit goes first, and every
four-ones word is valid either way round. The decoder reads both orders and
keeps the one in which the phasing signals (alpha/beta) and the "ZCZC" that
opens every message appear.
"""

import numpy as np

from .fsk import FSKDemod

# 7-bit code (as written, most significant first) -> (letters, figures).
CCIR476 = {
    0x0F: ("ALPHA", "ALPHA"), 0x17: ("J", "\a"), 0x1B: ("F", "!"), 0x1D: ("C", ":"),
    0x1E: ("K", "("), 0x27: ("W", "2"), 0x2B: ("Y", "6"), 0x2D: ("P", "0"), 0x2E: ("Q", "1"),
    0x33: ("BETA", "BETA"), 0x35: ("G", "&"), 0x36: ("FIGS", "FIGS"), 0x39: ("M", "."),
    0x3A: ("X", "/"), 0x3C: ("V", "="), 0x47: ("A", "-"), 0x4B: ("S", "'"), 0x4D: ("I", "8"),
    0x4E: ("U", "7"), 0x53: ("D", "\x05"), 0x55: ("R", "4"), 0x56: ("E", "3"), 0x59: ("N", ","),
    0x5A: ("LTRS", "LTRS"), 0x5C: (" ", " "), 0x63: ("Z", "+"), 0x65: ("L", ")"),
    0x66: ("RQ", "RQ"), 0x69: ("H", "#"), 0x6A: ("BLANK", "BLANK"), 0x6C: ("\n", "\n"),
    0x71: ("O", "9"), 0x72: ("B", "?"), 0x74: ("T", "5"), 0x78: ("\r", "\r"),
}
ALPHA, BETA = 0x0F, 0x33
PHASING = {ALPHA, BETA, 0x78, 0x66}     # alpha and beta, either bit order
LETTER_CODE = {v[0]: k for k, v in CCIR476.items()}
FIGURE_CODE = {v[1]: k for k, v in CCIR476.items() if v[1] != v[0]}


def reverse7(w: int) -> int:
    return int(f"{w:07b}"[::-1], 2)


def weight(w: int) -> int:
    return bin(w).count("1")


def encode(text: str, phasing_s=2.0) -> list[int]:
    """Words in transmission order (DX/RX interleaved) for *text*, for tests."""
    chars, shift = [], "L"
    for ch in text.upper():
        if ch in LETTER_CODE and ch not in ("ALPHA", "BETA", "RQ", "BLANK", "LTRS", "FIGS"):
            if shift != "L" and ch not in " \r\n":
                chars.append(LETTER_CODE["LTRS"])
                shift = "L"
            chars.append(LETTER_CODE[ch])
        elif ch in FIGURE_CODE:
            if shift != "F":
                chars.append(LETTER_CODE["FIGS"])
                shift = "F"
            chars.append(FIGURE_CODE[ch])
    pairs = int(phasing_s * 100 / 14)
    words = [ALPHA, BETA] * pairs
    dx = chars + [ALPHA] * 3
    for k, c in enumerate(dx):
        words.append(c)
        words.append(dx[k - 2] if k >= 2 else BETA)
    return words


class NavtexDecoder:
    BAUD = 100.0

    def __init__(self, rate, center=1700.0, shift=170.0):
        self.rate = rate
        self.demod = FSKDemod(rate, center + shift / 2, center - shift / 2, self.BAUD)
        self.spb = rate / self.BAUD
        self._buf = np.zeros(0)
        self._t = 0.0                    # start of the next bit in _buf
        self._prev = 0.0
        self.bits: list[int] = []
        self.locked = False
        self._offset = 0                 # bit offset of word 0 within self.bits
        self._invert = False
        self._words: list[int] = []      # aligned 7-bit words (as received, first bit = MSB)
        self._order_score = [0, 0]       # evidence for [as written, reversed]
        self._done = 0                   # DX slots already turned into text
        self._parity = 0
        self._shift = "L"
        self.errors = 0
        self.strength = 0.0

    # ---- bits ----------------------------------------------------------

    def feed(self, audio) -> str:
        soft, share = self.demod.process(audio)
        if len(share):
            self.strength = float(np.median(share))
        self._buf = np.concatenate([self._buf, soft])
        spb = self.spb
        while self._t + 2 * spb < len(self._buf):
            a = self._t
            v = float(np.mean(self._buf[int(a + 0.25 * spb) : int(a + 0.75 * spb)]))
            if (v > 0) != (self._prev > 0):
                # A transition: it should sit on the bit boundary at a.
                lo, hi = int(a - spb / 2), int(a + spb / 2)
                seg = self._buf[max(lo, 0) : hi]
                cross = np.flatnonzero(np.diff(np.sign(seg)) != 0)
                if len(cross):
                    pos = max(lo, 0) + cross[np.argmin(np.abs(max(lo, 0) + cross - a))]
                    self._t += 0.2 * (pos - a)
            self._prev = v
            self.bits.append(1 if v > 0 else 0)
            self._t += spb
        if self._t > 4 * self.rate:
            cut = int(self._t) - int(spb)
            self._buf = self._buf[cut:]
            self._t -= cut
        return self._words_to_text()

    # ---- words ---------------------------------------------------------

    def _word_at(self, i):
        b = self.bits[i : i + 7]
        w = 0
        for x in b:
            w = (w << 1) | (x ^ self._invert)
        return w

    def _try_lock(self):
        n = len(self.bits)
        if n < 7 * 16:
            return
        best = None
        for invert in (False, True):
            for off in range(7):
                start = n - 7 * 16 + off - 7
                if start < 0:
                    continue
                good = phasing = 0
                for k in range(16):
                    b = self.bits[start + 7 * k : start + 7 * k + 7]
                    w = sum((x ^ invert) << (6 - j) for j, x in enumerate(b))
                    good += weight(w) == 4
                    phasing += w in PHASING
                # The phasing signal repeats every 14 bits, and some wrong
                # alignments of it are four-ones words too: prefer the one
                # that reads as alpha/beta.
                score = good + phasing
                if best is None or score > best[0]:
                    best = (score, invert, start, good)
        if best and best[3] >= 15:
            self.locked = True
            self._invert = best[1]
            self._offset = best[2]
            self._words = []
            self._done = 0
            self._order_score = [0, 0]

    def _words_to_text(self) -> str:
        if not self.locked:
            self._try_lock()
            if not self.locked:
                # Drop old bits while searching.
                if len(self.bits) > 7 * 200:
                    del self.bits[: len(self.bits) - 7 * 40]
                return ""
        while self._offset + 7 * (len(self._words) + 1) <= len(self.bits):
            w = self._word_at(self._offset + 7 * len(self._words))
            self._words.append(w)
            if weight(w) == 4:
                if w in (ALPHA, BETA):
                    self._order_score[0] += 1
                if reverse7(w) in (ALPHA, BETA):
                    self._order_score[1] += 1
        recent = self._words[-30:]
        if len(recent) >= 30 and sum(weight(w) == 4 for w in recent) < 15:
            # Lost the signal: search again.
            self.locked = False
            del self.bits[: max(0, len(self.bits) - 7 * 20)]
            self._words = []
            return ""
        return self._text()

    def _code(self, w):
        return w if self._order_score[0] >= self._order_score[1] else reverse7(w)

    def _text(self) -> str:
        words = self._words
        # Which slots are DX: during phasing, alpha is sent in the DX slots;
        # in text, the copy five slots on agrees in the right parity.
        alpha = ALPHA if self._order_score[0] >= self._order_score[1] else reverse7(ALPHA)  # as received
        votes = [0, 0]
        for i in range(max(0, len(words) - 40), len(words)):
            if words[i] == alpha:
                votes[i % 2] += 1
            elif i + 5 < len(words) and weight(words[i]) == 4 and words[i] == words[i + 5] \
                    and words[i] not in PHASING:
                votes[i % 2] += 2
        if abs(votes[0] - votes[1]) >= 3:
            self._parity = 0 if votes[0] > votes[1] else 1
        elif self._done == 0:
            return ""      # not sure yet which slots are which
        out = []
        i = max(self._done, 0)
        if i % 2 != self._parity:
            i += 1
        while i + 5 < len(words):
            dx, rx = words[i], words[i + 5]
            w = dx if weight(dx) == 4 else rx if weight(rx) == 4 else None
            if w is None:
                self.errors += 1
                out.append("_")
            else:
                ch = self._char(self._code(w))
                if ch:
                    out.append(ch)
            i += 2
        self._done = i
        return "".join(out)

    def _char(self, code):
        letters, figures = CCIR476[code]
        if letters == "LTRS":
            self._shift = "L"
        elif letters == "FIGS":
            self._shift = "F"
        elif letters in ("ALPHA", "BETA", "RQ", "BLANK", "\r"):
            pass
        else:
            return letters if self._shift == "L" else figures
        return ""


def to_audio(words, rate, center=1700.0, shift=170.0, lsb_first=False, invert=False):
    """FSK audio for test words (100 baud, continuous phase)."""
    spb = rate / 100.0
    bits = []
    for w in words:
        b = [(w >> (6 - j)) & 1 for j in range(7)]
        bits += b[::-1] if lsb_first else b
    if invert:
        bits = [1 - b for b in bits]
    n = int(len(bits) * spb)
    idx = np.minimum((np.arange(n) / spb).astype(int), len(bits) - 1)
    f = center + np.where(np.array(bits)[idx] == 1, shift / 2, -shift / 2)
    return np.sin(2 * np.pi * np.cumsum(f) / rate).astype(np.float32) * 0.5
