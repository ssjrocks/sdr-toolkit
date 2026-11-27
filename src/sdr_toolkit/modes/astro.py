"""Radio astronomy pages: the galaxy's 21 cm hydrogen line, and meteor scatter (engines/astro.py)."""

import json
import time

import numpy as np

from .. import system
from .base import Event, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode

H1 = 1420.405751e6


def plot(v, y, width=900, height=420, title=""):
    """A line plot as pixels: velocity (km/s) across, excess (%) up; gridlines every 50 km/s and at 0 %."""
    img = np.full((height, width, 3), 250, np.uint8)
    if not len(v):
        return img
    v, y = np.asarray(v, float), np.asarray(y, float)
    x0, x1 = float(v.min()), float(v.max())
    lo, hi = min(-0.5, float(np.percentile(y, 1))), max(1.0, float(y.max()) * 1.15)
    m = 30

    def px(xv):
        return (m + (xv - x0) / (x1 - x0 + 1e-12) * (width - 2 * m)).astype(int)

    def py(yv):
        return (height - m - (yv - lo) / (hi - lo + 1e-12) * (height - 2 * m)).astype(int)

    for g in np.arange(np.ceil(x0 / 50) * 50, x1 + 1, 50):
        gx = int(px(np.array([g]))[0])
        img[m:height - m, gx] = (215, 215, 215) if g else (150, 150, 220)
    zy = int(py(np.array([0.0]))[0])
    if m <= zy < height - m:
        img[zy, m:width - m] = (150, 150, 150)
    xs, ys = px(v), py(y)
    for i in range(1, len(xs)):
        a, b = sorted((ys[i - 1], ys[i]))
        img[max(a, 0):min(b, height - 1) + 1, max(xs[i], 0):min(xs[i], width - 1) + 1] = (20, 70, 160)
    img[:, :2] = img[:, -2:] = img[:2] = img[-2:] = 120
    return img


class HydrogenLineMode(RadioMode):
    id = "hline"
    name = "Hydrogen line (21 cm, 1420 MHz)"
    group = "Satellites"
    summary = ("The galaxy's neutral hydrogen at 1420.406 MHz: a long average of the spectrum, the receiver's own "
               "shape taken away, and the hydrogen's glow against radial velocity (the spiral arms' motion).")
    output = "image"
    requires = ("rtl-sdr",)
    aspect = None
    settings = RADIO_SETTINGS + (
        Setting("rate", "Sample rate", 2400000, "choice", restart=True, per_sdr=True,
                choices=((2400000, "2.4 MS/s (±170 km/s: an RTL-SDR)"), (4800000, "4.8 MS/s (±350 km/s: HackRF)"))),
        Setting("every", "Update every", 10, "int", 2, 600, 1, suffix=" s", restart=True),
        Setting("file_rate", "Recording sample rate", 2400000, "int", 240000, 20000000, 1000, suffix=" Hz",
                restart=True, help="A recording centred on 1420.406 MHz."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ centred on 1420.406 MHz, averaged for as long as it runs",
        bandwidth="The line spreads over about ±200 km/s (±1 MHz)",
        frequencies=(("1420.406 MHz", "neutral hydrogen (HI), 21 cm"),),
        notes="Needs gain: an LNA for 1.4 GHz (with its bias tee) and an antenna with a narrow beam (a horn, a "
              "small dish, or a cantenna) pointed at the Milky Way, outdoors. Minutes of averaging show it; the "
              "excess is a few % over the receiver's noise. Velocities aren't corrected for the Earth's motion. "
              "Bumps outside ±180 km/s (or very narrow ones) are local interference, not hydrogen.",
    )

    def _rate(self):
        return int(self.values["rate"]) if self.live() else int(self.values["file_rate"])

    def listening_on(self):
        return "1420.406 MHz"

    def upstream(self):
        if not self.live():
            return None
        if self.bridge():
            return self.relay_stream(H1, self._rate())
        cmd = ["rtl_sdr", "-d", self.device_index().lstrip(":"), "-f", str(int(H1)), "-s", str(self._rate()),
               "-p", str(self.values["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def command(self):
        self._last = None
        cmd = system.python_command("sdr_toolkit.engines.astro") + ["hline", "--rate", str(self._rate()),
                                                                    "--every", str(self.values["every"])]
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
        if e.get("event") != "spectrum":
            return []
        self._last = e
        title = (f"Hydrogen line · {e['seconds']:.0f} s averaged · peak {e['peak']:+.2f}% at {e['peak_v']:+.0f} km/s "
                 "(gridlines every 50 km/s)")
        return [Event("image", image=plot(e["v"], e["excess"]), title=title, done=False, row=None),
                Event("log", f"Hydrogen line after {e['seconds']:.0f} s: {e['peak']:+.2f}% at {e['peak_v']:+.0f} km/s",
                      style="dim")]

    def activity(self):
        e = getattr(self, "_last", None)
        return super().activity() + (f" · {e['seconds']:.0f} s averaged, peak {e['peak']:+.2f}%" if e else "")


class MeteorScatterMode(RadioMode):
    id = "meteors"
    name = "Meteor scatter"
    group = "Satellites"
    summary = ("Meteors heard by their trails: a transmitter beyond your horizon (a radar, a distant TV or FM "
               "carrier) pings in for a moment when a meteor's ionised trail reflects it. Each ping is listed.")
    output = "table"
    requires = ("rtl-sdr",)
    key_column = "n"
    columns = (("n", "#"), ("when", "When"), ("duration", "Length s"), ("snr", "Strength dB"),
               ("doppler", "Doppler Hz"))
    settings = RADIO_SETTINGS + (
        Setting("freq_mhz", "Transmitter frequency", 143.05, "float", 24.0, 1800.0, 0.0001, 4, suffix=" MHz",
                restart=True, help="A strong transmitter 500-2000 km away that you can't hear directly: the GRAVES "
                                   "radar (143.050 MHz) in Europe, a distant FM or TV carrier elsewhere."),
        Setting("threshold", "Ping threshold", 10, "int", 3, 40, 1, suffix=" dB", restart=True),
        Setting("file_rate", "Recording sample rate", 240000, "int", 48000, 3200000, 1000, suffix=" Hz",
                restart=True),
        Setting("file_offset", "Carrier offset in recording", 0, "int", -1600000, 1600000, 100, suffix=" Hz",
                restart=True),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ 240 kS/s, tuned 60 kHz above the transmitter; its carrier watched in a ±1.5 kHz window",
        bandwidth="A ping is a narrow carrier, shifted a few hundred Hz by the trail's motion",
        frequencies=(("143.050 MHz", "GRAVES space-surveillance radar, France (Europe-wide pings)"),
                     ("50-110 MHz", "distant TV or FM broadcast carriers")),
        notes="Pings come every few minutes on an ordinary night, many a minute in a shower (Perseids, Geminids); "
              "most around dawn. An antenna pointed towards the transmitter, outdoors, helps a lot.",
    )

    def listening_on(self):
        return f"{float(self.values['freq_mhz']):g} MHz"

    def upstream(self):
        if not self.live():
            return None
        f = float(self.values["freq_mhz"]) * 1e6
        if self.bridge():
            return self.relay_stream(f + 60000, 240000)
        cmd = ["rtl_sdr", "-d", self.device_index().lstrip(":"), "-f", str(int(f + 60000)), "-s", "240000",
               "-p", str(self.values["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def command(self):
        self._pings = []
        self._started = time.time()
        rate = 240000 if self.live() else int(self.values["file_rate"])
        offset = -60000 if self.live() else float(self.values["file_offset"])
        cmd = system.python_command("sdr_toolkit.engines.astro") + [
            "meteors", "--rate", str(rate), "--offset", str(offset), "--threshold", str(self.values["threshold"])]
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
        if e.get("event") != "ping":
            return []
        self._pings.append(e)
        n = len(self._pings)
        when = (time.strftime("%H:%M:%S", time.localtime(e["wall"])) if self.live()
                else f"{e['t']:.1f} s into the recording")
        row = {"n": n, "when": when, "duration": f"{e['duration']:.2f}", "snr": f"{e['snr']:.0f}",
               "doppler": f"{e['doppler']:+.0f}"}
        return [Event("row", data=row),
                Event("log", f"Meteor ping {n}: {e['duration']:.2f} s, {e['snr']:.0f} dB, {e['doppler']:+.0f} Hz")]

    def activity(self):
        pings = getattr(self, "_pings", [])
        hours = max((time.time() - getattr(self, "_started", time.time())) / 3600, 1 / 60)
        return super().activity() + f" · {len(pings)} pings" + (f" ({len(pings) / hours:.0f} an hour)" if pings else "")
