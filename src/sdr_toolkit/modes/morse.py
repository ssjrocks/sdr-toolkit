"""Morse (CW and MCW): the Morse Decoder app's engine."""

import time

from ..engines.morse.engine import Engine
from .base import Event, Mode, Setting, Tuning

SPEEDS = (("any", "Auto, any speed"), ("radio", "Auto, Talkpod speeds (5-40 WPM steps)")) + tuple(
    (str(w), f"{w} WPM") for w in range(5, 61, 5)
)


def pretty(code: str) -> str:
    return code.replace(".", "·").replace("-", "−")


class MorseMode(Mode):
    id = "morse"
    name = "Morse / CW"
    group = "Text"
    summary = "On-off keyed tone: CW on HF, MCW (a keyed tone on FM) such as the Talkpod's Morse screen."
    rate = 8000
    settings = (
        Setting("tone", "Tone", 700, "int", 200, 2500, 10, suffix=" Hz",
                help="Audio pitch of the Morse. Click its peak on the spectrum to set it."),
        Setting("bandwidth", "Filter width", 100, "int", 30, 400, 10, suffix=" Hz",
                help="Narrower rejects more noise and nearby signals but needs an accurate tone; "
                     "wider follows drift and speeds above ~40 WPM."),
        Setting("speed", "Speed", "any", "choice", choices=SPEEDS,
                help="Auto measures the speed from the dits and dahs. A fixed WPM settles "
                     "messages of only dits or only dahs (a lone E or T)."),
        Setting("sensitivity", "Sensitivity", 5, "int", 1, 10,
                help="Raise for weak signals, lower if noise makes false dits."),
    )
    tuning = Tuning(
        sdr_mode="CW (or USB) for HF Morse; FM for MCW (a tone keyed on an FM carrier, e.g. the Talkpod)",
        bandwidth="CW: 300–500 Hz is plenty, wider if you need to find the signal. FM: narrow (12.5 kHz).",
        frequencies=(
            ("3.500–3.570 MHz", "80 m CW"), ("7.000–7.040 MHz", "40 m CW"),
            ("10.100–10.130 MHz", "30 m CW"), ("14.000–14.070 MHz", "20 m CW"),
            ("Your FM channel", "Talkpod MCW: the slot's transmit frequency"),
        ),
        notes="On CW/USB the tone you hear is the offset from the dial: tune so the beep sits "
              "at the Tone setting (or click its peak on the spectrum). The Talkpod sends 700 Hz.",
    )

    def start(self, values):
        super().start(values)
        self.engine = Engine(values["tone"], values["bandwidth"], self.rate)
        self._configure()

    def apply(self, values):
        super().apply(values)
        self._configure()

    def _configure(self):
        v = self.values
        self.engine.detector.set_tone(v["tone"], v["bandwidth"])
        self.engine.keyer.set_sensitivity(0.9 - 0.06 * v["sensitivity"])
        dec = self.engine.decoder
        if v["speed"] in ("any", "radio"):
            dec.mode = v["speed"]
        else:
            dec.mode, dec.fixed_wpm = "fixed", int(v["speed"])
            dec.last_unit = 1200 // dec.fixed_wpm

    def feed(self, audio, now):
        events = []
        for msg in self.engine.feed(audio, now):
            stamp = time.strftime("%H:%M:%S", time.localtime(msg.start))
            events.append(Event("line", f"{stamp}  {msg.text}"))
            events.append(Event("line", f"          {pretty(msg.code)}  · {self._speed(msg)}", style="dim"))
            events.append(Event("live", ""))
        cur = self.engine.current()
        if cur is not None:
            events.append(Event("live", f"{cur.text}   {pretty(cur.code)}   ({self._speed(cur)})"))
        return events

    @staticmethod
    def _speed(msg):
        fit = msg.fit
        w = fit.radio_wpm
        speed = f"{w} WPM" if w else f"{fit.wpm:.1f} WPM"
        return f"{speed}, unit {fit.unit:.0f} ms, timing error {fit.error * 100:.0f}%"

    def markers(self):
        return [(self.values["tone"], "tone")]

    def set_frequency(self, hz):
        self.values["tone"] = int(round(hz / 10) * 10)
        return "tone"
