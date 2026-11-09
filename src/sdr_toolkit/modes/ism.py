"""ISM-band devices (weather sensors, tyre sensors, doorbells, remotes...), decoded by rtl_433."""

import json
import re
import time

from .base import Event, Setting, Tuning
from .process import looks_like_error
from .radio import RADIO_SETTINGS, RadioMode

SHOWN = ("time", "model", "id", "channel", "battery_ok", "mic", "mod", "freq", "rssi", "snr", "noise")


class ISMMode(RadioMode):
    id = "ism"
    name = "ISM sensors (rtl_433)"
    group = "RTL-SDR"
    summary = ("Weather stations, thermometers, tyre-pressure sensors, doorbells, remotes and meters on "
               "315/433/868/915 MHz: over 250 device types. Decoded by rtl_433.")
    output = "table"
    columns = (("time", "Time"), ("model", "Device"), ("id", "ID"), ("channel", "Ch"),
               ("data", "Readings"), ("signal", "Signal"))
    requires = ("rtl-433",)
    iq_rate = 250000
    settings = RADIO_SETTINGS + (
        Setting("freq", "Frequency", "433.92M", "choice", restart=True,
                choices=(("433.92M", "433.92 MHz (worldwide)"), ("915M", "915 MHz (Americas, Australia)"),
                         ("868.3M", "868.3 MHz (Europe)"), ("315M", "315 MHz (North America)"),
                         ("345M", "345 MHz (security sensors, US)"), ("custom", "Other (below)")),
                help="Most sensors use 433.92 MHz. Which other bands are used depends on your region."),
        Setting("custom_freq", "Other frequency", "433.92M", "text", restart=True,
                help="With 'Other': e.g. 434.1M, or several separated by spaces to hop between them."),
        Setting("hop", "Hop every", 600, "int", 10, 3600, 10, suffix=" s", restart=True,
                help="With several frequencies: how long to stay on each."),
        Setting("rate", "Sample rate", "250k", "choice", restart=True,
                choices=(("250k", "250 kHz (most devices)"), ("1024k", "1.024 MHz (wide FSK devices)")),
                help="Also the rate a recording must have been made at."),
        Setting("protocols", "Only protocols", "", "text", restart=True,
                help="rtl_433 protocol numbers, space-separated (e.g. '19 73'); empty decodes all "
                     "default ones. 'rtl_433 -R help' lists them."),
        Setting("level", "Show signal level", True, "bool", help="RSSI and SNR of each message."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR (the toolkit tunes it). A WebSDR can't be used: these need the radio samples.",
        bandwidth="250 kHz around the frequency (1 MHz for wide FSK devices)",
        frequencies=(("433.92 MHz", "Weather sensors, remotes, doorbells (worldwide)"),
                     ("915 MHz", "Sensors and meters in the Americas and Australia/NZ"),
                     ("868.3 MHz", "Europe"), ("315 MHz", "North America: tyre sensors, car remotes")),
        notes="Sensors transmit every 30-60 s, so give it a minute. The antenna that came with the "
              "dongle works for nearby devices; a 433 MHz quarter-wave (17 cm) reaches the street.",
    )

    def freqs(self) -> list[str]:
        f = self.values["freq"]
        return self.values["custom_freq"].split() if f == "custom" else [f]

    def command(self):
        v = self.values
        # -F log: rtl_433's own messages (no device, errors), which -F json alone keeps quiet.
        # noise:5 and stats:1:10 report the noise floor and how many transmissions were seen,
        # so the window can show it's listening even when nothing decodes.
        cmd = ["rtl_433", "-F", "json", "-F", "log", "-M", "time:iso", "-M", "level", "-M", "noise:5",
               "-M", "stats:1:10", "-s", v["rate"]]
        self._decoded = self._seen = 0
        self._noise = None
        for p in v["protocols"].split():
            cmd += ["-R", p]
        if self.live():
            cmd += ["-d", self.device_index(), "-p", str(v["ppm"])]
            if self.gain_db():
                cmd += ["-g", self.gain_db()]
            for f in self.freqs():
                cmd += ["-f", f]
            if len(self.freqs()) > 1:
                cmd += ["-H", str(v["hop"])]
        else:
            cmd += ["-r", f"cu8:{self.file_path()}"]
        return cmd

    def parse(self, line):
        line = line.strip()
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            if self._problem:
                return []
            m = re.search(r"estimated noise (-?[\d.]+) dB", line)
            if m:
                self._noise = float(m.group(1))
                return []
            if looks_like_error(line):
                return [Event("status", line), Event("line", line, style="warn")]
            return []
        try:
            d = json.loads(line)
        except ValueError:
            return []
        if "model" not in d:
            if isinstance(d.get("frames"), dict):           # a stats report
                self._seen += int(d["frames"].get("count", 0))
            return []
        self._decoded += 1
        t = str(d.get("time", ""))
        stamp = t[11:19] if len(t) >= 19 and t[4] == "-" else time.strftime("%H:%M:%S")
        readings = ", ".join(f"{k}={v}" for k, v in d.items() if k not in SHOWN)
        if "battery_ok" in d:
            readings += ("" if not readings else ", ") + ("battery OK" if d["battery_ok"] else "battery LOW")
        signal = ""
        if self.values["level"] and "rssi" in d:
            signal = f"{d['rssi']:.0f} dB, SNR {d.get('snr', 0):.0f}"
            if "freq" in d:
                signal += f", {d['freq']:.3f} MHz"
        row = {"time": stamp, "model": d.get("model", ""), "id": d.get("id", ""),
               "channel": d.get("channel", ""), "data": readings, "signal": signal}
        return [Event("row", data=row)]

    def listening_on(self):
        return " + ".join(f.rstrip("M") for f in self.freqs()) + " MHz"

    def activity(self) -> str:
        secs = int(time.time() - self._t0)
        parts = [super().activity()]
        if self._noise is not None:
            parts.append(f"noise {self._noise:.0f} dB")
        parts.append(f"{self._seen} transmission{'s' if self._seen != 1 else ''} seen, {self._decoded} decoded")
        text = " · ".join(parts)
        if self.live() and secs > 120 and self._seen == 0 and self.freqs() != ["433.92M"]:
            text += ". Nothing is transmitting here; most sensors use 433.92 MHz"
        return text

    def finished(self, code):
        if not self.live():
            return [Event("status", "recording finished")]
        return super().finished(code)
