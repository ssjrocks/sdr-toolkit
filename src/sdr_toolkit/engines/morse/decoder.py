"""Morse timing: tone on/off runs in, text out.

Each transmission (tone runs until a long silence) is re-decoded as a whole
whenever a run ends: the unit and an edge bias are fitted to every run so far,
so the text firms up as the message goes. Classification follows the Talkpod
Super Firmware's docs/morse.md: on < 2 units is a dit, otherwise a dah; off
< 2 units is inside a letter, 2-5 ends a letter, > 5 is a word space.
"""

import math
from dataclasses import dataclass, field

import numpy as np

CODE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "'": ".----.", "!": "-.-.--",
    "/": "-..-.", "(": "-.--.", ")": "-.--.-", "&": ".-...", ":": "---...",
    ";": "-.-.-.", "=": "-...-", "+": ".-.-.", "-": "-....-", "_": "..--.-",
    '"': ".-..-.", "@": ".--.-.",
}
LETTERS = {code: ch for ch, code in CODE.items()}

# The radio's speeds: 5-40 WPM in steps of 5, unit = floor(1200 / WPM) ms.
RADIO_WPM = list(range(5, 45, 5))
RADIO_UNITS = [1200 // w for w in RADIO_WPM]
DEFAULT_UNIT = 40  # 30 WPM, the radio's starting speed

_ON_LEVELS = np.log([1.0, 3.0])
_OFF_LEVELS = np.log([1.0, 3.0, 7.0])
_BIAS_STEPS = np.linspace(-0.4, 0.4, 17)
_ANY_UNITS = np.geomspace(20, 300, 160)


def wpm_for_unit(unit: float) -> float:
    return 1200.0 / unit


@dataclass
class Fit:
    unit: float          # ms
    bias: float          # ms added to every tone run by the receive chain
    error: float         # rms log error of the runs against 1/3/7 units

    @property
    def wpm(self) -> float:
        return wpm_for_unit(self.unit)

    @property
    def radio_wpm(self) -> int | None:
        """The radio setting this unit matches, if it is one of them."""
        for w, u in zip(RADIO_WPM, RADIO_UNITS):
            if abs(self.unit - u) < 0.5:
                return w
        return None


def fit_timing(runs: list[tuple[bool, int]], units, prior: float) -> Fit:
    """Choose the unit and edge bias that best fit the runs.

    Runs are (tone_on, ms) and must start with a tone. The radio's word gap is
    always 7 units (several spaces still make one gap), which is what tells
    "HI" at 5 WPM (gaps of 3) from "TTTT TT" at 15 WPM (gaps of 9). A small
    pull towards *prior* settles what timing can't: a lone "E" or "T".
    """
    units = np.asarray(units, dtype=float)
    on = np.array([d for s, d in runs if s], dtype=float)
    off = np.array([d for s, d in runs if not s], dtype=float)
    u = units[:, None, None]
    b = _BIAS_STEPS[None, :, None] * u
    data = np.zeros((len(units), len(_BIAS_STEPS)))
    for durs, levels, sign in ((on, _ON_LEVELS, -1), (off, _OFF_LEVELS, 1)):
        if len(durs):
            r = np.log(np.maximum((durs[None, None, :] + sign * b) / u, 0.05))
            err = np.min(np.abs(r[..., None] - levels), axis=-1)
            # Capped, so one glitch can't outweigh the rest of the message.
            data += np.sum(np.minimum(err, 0.5) ** 2, axis=-1)
    cost = data + 0.005 * np.log(units / prior)[:, None] ** 2
    cost += 0.01 * _BIAS_STEPS[None, :] ** 2
    i, j = np.unravel_index(np.argmin(cost), cost.shape)
    unit = float(units[i])
    n = max(len(on) + len(off), 1)
    return Fit(unit, float(_BIAS_STEPS[j] * unit), math.sqrt(data[i, j] / n))


def decode_runs(runs: list[tuple[bool, int]], fit: Fit, trailing_off: int = 0):
    """Turn runs into (text, code). *trailing_off* is the silence after the last run.

    A letter that isn't followed by at least 2 units of silence yet is shown
    in the code but left out of the text.
    """
    text, code, letter = [], [], ""

    def end_letter():
        nonlocal letter
        if letter:
            text.append(LETTERS.get(letter, "#"))
            code.append(letter)
            letter = ""

    for tone, dur in runs:
        r = (dur - fit.bias if tone else dur + fit.bias) / fit.unit
        if tone:
            letter += "." if r < 2 else "-"
        elif r >= 5:
            end_letter()
            text.append(" ")
            code.append("/")
        elif r >= 2:
            end_letter()
    if letter and (trailing_off + fit.bias) / fit.unit >= 2:
        end_letter()
        letter = ""
    shown = " ".join(code) + (" " + letter if letter else "")
    return "".join(text).strip(), shown.strip()


@dataclass
class Message:
    text: str
    code: str
    fit: Fit
    start: float          # time.time() of the first tone
    duration_ms: int
    final: bool = False


@dataclass
class MorseDecoder:
    """Collects keyer runs into transmissions and decodes them."""

    mode: str = "radio"           # "radio" (Talkpod speeds), "any", or "fixed"
    fixed_wpm: int = 30
    last_unit: float = DEFAULT_UNIT
    runs: list = field(default_factory=list)
    start: float | None = None
    fit: Fit | None = None

    def candidates(self):
        if self.mode == "fixed":
            return [1200 // self.fixed_wpm]
        if self.mode == "any":
            return _ANY_UNITS
        return RADIO_UNITS

    def end_gap_ms(self) -> float:
        unit = self.fit.unit if self.fit else self.last_unit
        return max(1200.0, 12 * unit)

    def add_run(self, tone: bool, dur: int, now: float) -> None:
        """Add a finished run. Silence before a transmission is dropped."""
        if not self.runs and not tone:
            return
        if not self.runs:
            self.start = now - dur / 1000.0
        self.runs.append((tone, dur))
        self._refit()

    def _refit(self) -> None:
        runs = self._clean(self.runs)
        if runs:
            self.fit = fit_timing(runs, self.candidates(), self.last_unit)

    def _clean(self, runs):
        """Merge tone and gap blips shorter than a quarter unit into their neighbours."""
        unit = self.fit.unit if self.fit else self.last_unit
        limit = 0.25 * unit
        out = []
        for tone, dur in runs:
            if out and (dur < limit or out[-1][0] == tone):
                out[-1] = (out[-1][0], out[-1][1] + dur)
            elif not out and not tone:
                continue
            else:
                out.append((tone, dur))
        # Folding a short run into the previous one can leave two runs of the
        # same kind next to each other; join them.
        merged = []
        for tone, dur in out:
            if merged and merged[-1][0] == tone:
                merged[-1] = (tone, merged[-1][1] + dur)
            else:
                merged.append((tone, dur))
        return merged

    def current(self, silence_ms: int) -> Message | None:
        """The transmission in progress, decoded as far as it goes."""
        if not self.runs or not self.fit:
            return None
        runs = self._clean(self.runs)
        trailing = silence_ms
        if runs and not runs[-1][0]:
            trailing += runs[-1][1]
            runs = runs[:-1]
        text, code = decode_runs(runs, self.fit, trailing)
        total = sum(d for _, d in runs)
        return Message(text, code, self.fit, self.start, total)

    def finish(self) -> Message | None:
        """End the transmission (long silence); returns it if it held any tone.

        A lone tone shorter than 25 ms is taken as noise, not as an "E".
        """
        msg = self.current(10**6)
        if msg and msg.duration_ms < 25:
            msg = None
        if msg:
            msg.final = True
            self.last_unit = msg.fit.unit
        self.runs = []
        self.start = None
        self.fit = None
        return msg
