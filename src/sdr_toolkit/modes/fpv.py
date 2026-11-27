"""Analog FPV video at 5.8 GHz: the picture a drone's or an FPV camera's analog video transmitter sends (PAL or
NTSC, FM), shown as it comes. Needs a HackRF (5.8 GHz). Decoded by the toolkit (engines/fpv.py)."""

import base64
import json

import numpy as np

from .. import system
from ..engines.fpv import CHANNELS
from .base import Event, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode

RATE = 19_200_000
CHOICES = tuple((f"{b}{i + 1}", f"{b}{i + 1}: {mhz} MHz") for b, ch in CHANNELS.items() for i, mhz in enumerate(ch))


class FPVMode(RadioMode):
    id = "fpv"
    name = "Analog FPV video (5.8 GHz)"
    group = "Wi-Fi & Bluetooth"
    summary = ("The picture from an analog FPV video transmitter (drones, FPV cameras) on the 5.8 GHz bands, as it "
               "comes (black and white). Needs a HackRF.")
    output = "image"
    requires = ("rtl-sdr",)
    settings = RADIO_SETTINGS + (
        Setting("channel", "Channel", "R1", "choice", restart=True, choices=CHOICES,
                help="Raceband (R), A, B, E or F: the transmitter's channel. Spectrum & receiver's sweep shows "
                     "which carries a signal."),
        Setting("every", "Show every", 5, "int", 1, 50, 1, suffix=" fields", restart=True,
                help="50 fields come each second (PAL); showing fewer saves CPU."),
        Setting("file_rate", "Recording sample rate", RATE, "int", 8000000, 20000000, 100000, suffix=" Hz",
                restart=True),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ at 19.2 MS/s from a HackRF on the video channel, FM-demodulated",
        bandwidth="An analog video channel is about 20 MHz wide",
        frequencies=tuple((f"{b} band", ", ".join(str(m) for m in ch) + " MHz") for b, ch in CHANNELS.items()),
        notes="FPV video is low power (25-600 mW): heard within a few hundred metres of the drone or camera. "
              "Colour isn't decoded; digital FPV systems (DJI, Walksnail, HDZero) aren't analog and don't show.",
    )

    def _mhz(self):
        c = self.values["channel"]
        return CHANNELS[c[0]][int(c[1:]) - 1]

    def listening_on(self):
        return f"{self.values['channel']} ({self._mhz()} MHz)"

    def upstream(self):
        if not self.live():
            return None
        b = self.bridge()
        if b is None or b.device.get("type") != "hackrf":
            raise RuntimeError("5.8 GHz is out of an RTL-SDR's reach: this needs a HackRF (here, or on a relay).")
        return self.relay_stream(self._mhz() * 1e6, RATE)

    def command(self):
        self._fields = 0
        rate = RATE if self.live() else int(self.values["file_rate"])
        cmd = system.python_command("sdr_toolkit.engines.fpv") + ["--rate", str(rate), "--every",
                                                                  str(self.values["every"])]
        if not self.live():
            cmd += ["--input", self.file_path()]
        return cmd

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            return problem if problem is not None else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        if e.get("event") != "field":
            return []
        self._fields += 1
        g = np.frombuffer(base64.b64decode(e["gray"]), np.uint8).reshape(e["h"], e["w"])
        # Fields are half the lines of a frame: double them up so the picture has its shape.
        img = np.repeat(np.repeat(g, 2, axis=0)[:, :, None], 3, axis=2)
        title = f"{self.listening_on()} · {e['std']} · sync {e['sync']:.0f} dB"
        return [Event("image", image=img, title=title, done=False, row=None)]

    def activity(self):
        return super().activity() + f" · {getattr(self, '_fields', 0)} pictures"
