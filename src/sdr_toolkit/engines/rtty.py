"""RTTY: asynchronous Baudot (ITA2) on two-tone FSK.

Each character is a start bit (space), 5 data bits sent least significant
first, and 1-2 stop bits (mark). The decoder waits for the mark-to-space edge
of a start bit, checks the middle of the start bit is still space, samples
each data bit at its middle, and checks the stop bit.
"""

import numpy as np

from .fsk import FSKDemod

LTRS = 0x1F
FIGS = 0x1B
LETTERS = "\0E\nA SIU\rDRJNFCKTZLWHYPQOBG\0MXV\0"
FIGURES_US = "\0" + "3\n- \a87\r$4',!:(5\")2#6019?&\0./;\0"
FIGURES_ITA2 = "\0" + "3\n- '87\r\x054\a,!:(5+)2£6019?&\0./=\0"


def encode(text: str, figures="US") -> list[int]:
    """Baudot codes for *text* with shifts, starting in letters (for tests)."""
    figs = FIGURES_US if figures == "US" else FIGURES_ITA2
    out, shift = [LTRS, LTRS], "L"
    for ch in text.upper():
        if ch in LETTERS and ch not in "\0":
            if shift != "L" and ch not in " \r\n":
                out.append(LTRS)
                shift = "L"
            out.append(LETTERS.index(ch))
        elif ch in figs and ch not in "\0":
            if shift != "F":
                out.append(FIGS)
                shift = "F"
            out.append(figs.index(ch))
    return out


class RTTYDecoder:
    def __init__(self, rate, mark=2125.0, shift=170.0, baud=45.45, reverse=False,
                 stop_bits=1.5, unshift_on_space=True, figures="US", squelch=0.3):
        self.rate = rate
        # Amateur RTTY (LSB, or AFSK 2125/2295 Hz): space above mark. Reverse for USB.
        space = mark + shift if not reverse else mark - shift
        self.demod = FSKDemod(rate, mark, space, baud)
        self.spb = rate / baud                   # samples per bit
        self.stop_bits = stop_bits
        self.unshift = unshift_on_space
        self.figs = FIGURES_US if figures == "US" else FIGURES_ITA2
        self.shift = "L"
        self._buf = np.zeros(0)
        self._pos = 0                            # next sample to examine in _buf
        self._prev = 1.0
        self.squelch = squelch                   # least share of the audio in the tones
        self._share = np.zeros(0)

    def feed(self, audio) -> str:
        soft, share = self.demod.process(audio)
        self._buf = np.concatenate([self._buf, soft])
        self._share = np.concatenate([self._share, share])
        out = []
        frame = int(self.spb * (6 + min(self.stop_bits, 1.0)))
        while self._pos + frame < len(self._buf):
            v = self._buf[self._pos]
            if self._prev > 0 >= v and self._share[self._pos] >= self.squelch:
                # Mark-to-space edge: a start bit, if it's still space halfway.
                c = self._char(self._pos)
                if c is not None:
                    out.append(c)
                    # Skip to the end of the stop bit, less a little slack.
                    self._pos += int(self.spb * (6 + 0.5 * min(self.stop_bits, 1.0)))
                    self._prev = 1.0
                    continue
            self._prev = v
            self._pos += 1
        if self._pos > self.rate:
            self._buf = self._buf[self._pos - 1 :]
            self._share = self._share[self._pos - 1 :]
            self._pos = 1
        return "".join(out)

    def _bit(self, start, k):
        """Mean of the middle half of bit k after the start edge."""
        a = int(start + (k + 0.25) * self.spb)
        b = int(start + (k + 0.75) * self.spb)
        return float(np.mean(self._buf[a:b]))

    def _char(self, start):
        if self._bit(start, 0) > -0.2:
            return None                          # a glitch, not a start bit
        if self._bit(start, 6) < 0.0:
            return None                          # no stop bit: framing error
        code = 0
        for k in range(5):
            if self._bit(start, k + 1) > 0:
                code |= 1 << k
        if code == LTRS:
            self.shift = "L"
            return ""
        if code == FIGS:
            self.shift = "F"
            return ""
        table = LETTERS if self.shift == "L" else self.figs
        ch = table[code]
        if ch == " " and self.unshift:
            self.shift = "L"
        if ch == "\r" or ch == "\0":
            return ""
        return ch
