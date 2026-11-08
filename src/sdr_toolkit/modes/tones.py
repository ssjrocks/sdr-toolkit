"""DTMF digits, CTCSS and DCS on FM channels."""

import time

from ..engines.tones import ToneDecoder
from .base import Event, Mode, Setting, Tuning


class TonesMode(Mode):
    id = "tones"
    name = "DTMF / CTCSS / DCS"
    group = "Tones"
    summary = "Keypad digits, and the sub-audible tone or code a repeater or radio channel uses."
    rate = 8000
    settings = (
        Setting("dtmf", "DTMF digits", True, "bool", restart=True),
        Setting("min_ms", "Shortest digit", 40, "int", 20, 200, 5, suffix=" ms", restart=True,
                help="Digits shorter than this are ignored (speech can briefly look like DTMF)."),
        Setting("gap", "New line after", 2.0, "float", 0.5, 10, 0.5, 1, suffix=" s",
                help="A pause this long between digits starts a new line."),
        Setting("ctcss", "CTCSS tones", True, "bool", restart=True),
        Setting("dcs", "DCS codes", True, "bool", restart=True),
    )
    tuning = Tuning(
        sdr_mode="FM (narrow)",
        bandwidth="12.5 kHz",
        frequencies=(("Repeater outputs", "e.g. 146–148 MHz and 438–440 MHz amateur repeaters"),
                     ("Your own radio's channel", "to check the tone or code it sends")),
        notes="CTCSS (67–254 Hz) and DCS sit below the voice. Many receivers and WebSDRs filter "
              "audio below 300 Hz, which removes them: if voice is heard but no tone shows, that's "
              "the likely cause. DCS has no fixed polarity over the air, and one code's bit stream "
              "also reads as other codes; the equivalents are listed.",
    )

    def start(self, values):
        super().start(values)
        v = self.values
        self.dec = ToneDecoder(self.rate, v["dtmf"], v["ctcss"], v["dcs"], v["min_ms"])
        self._digits, self._last = "", 0.0

    def feed(self, audio, now):
        events = []
        for kind, value in self.dec.feed(audio):
            stamp = time.strftime("%H:%M:%S", time.localtime(now))
            if kind == "dtmf":
                if self._digits and now - self._last > float(self.values["gap"]):
                    events.append(Event("line", self._digits))
                    self._digits = ""
                if not self._digits:
                    self._digits = f"{stamp}  DTMF  "
                self._digits += value
                self._last = now
            elif kind == "ctcss":
                events.append(Event("line", f"{stamp}  CTCSS {value:.1f} Hz" if value else f"{stamp}  CTCSS gone",
                                    style="" if value else "dim"))
            elif kind == "dcs":
                events.append(Event("line", f"{stamp}  DCS {value}" if value else f"{stamp}  DCS gone",
                                    style="" if value else "dim"))
        if self._digits and now - self._last > float(self.values["gap"]):
            events.append(Event("line", self._digits))
            self._digits = ""
        if self._digits:
            events.append(Event("live", self._digits))
        elif events:
            events.append(Event("live", ""))
        parts = []
        if self.dec.ctcss_now:
            parts.append(f"CTCSS {self.dec.ctcss_now:.1f} Hz")
        if self.dec.dcs_now:
            parts.append(f"DCS {self.dec.dcs_now}")
        events.append(Event("status", " · ".join(parts) if parts else "no tone or code"))
        return events

    def markers(self):
        return [(697, ""), (941, "DTMF"), (1209, ""), (1633, "")]
