"""Your position from the GPS satellites, received by the RTL-SDR and solved by GNSS-SDR.

The fix fills in "Your location" for the other RTL-SDR modes. A recording is
unsigned 8-bit IQ at 2.048 MS/s on 1575.42 MHz (``rtl_sdr -f 1575420000 -s
2048000``); it's turned into the signed bytes GNSS-SDR reads, on the way in.
"""

import math
import re
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np

from ..engines import gnss
from .base import Event, Setting, Tuning
from .radio import LOCATION_SETTINGS, RADIO_SETTINGS, RadioMode

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _moved_m(a, b) -> float:
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


class GPSFixMode(RadioMode):
    id = "gps"
    name = "GPS position fix (1575 MHz)"
    group = "RTL-SDR"
    summary = ("Works out where you are from the GPS satellites' own signals, and fills in your location "
               "for the other RTL-SDR modes. Solved by GNSS-SDR.")
    output = "table"
    map = True
    columns = (("prn", "Satellite"), ("channel", "Channel"), ("cn0", "Signal dB-Hz"), ("nav", "Orbit data"),
               ("state", "State"))
    requires = ("gnss-sdr",)
    iq_rate = gnss.RATE
    settings = RADIO_SETTINGS + LOCATION_SETTINGS + (
        Setting("doppler", "Search range", 10, "int", 5, 150, 5, suffix=" kHz", restart=True,
                help="How far from 1575.42 MHz to look for each satellite. ±10 kHz covers the satellites' "
                     "movement once the frequency correction is right; without it a cheap dongle is "
                     "50-100 kHz off, and a wider search is much slower."),
        Setting("channels", "Satellites at once", 8, "int", 4, 12, restart=True,
                help="Channels tracking a satellite each. A fix needs 4; more gives a better one and "
                     "takes more CPU."),
        Setting("fill", "Fill in my location", True, "bool",
                help="Save the fix (averaged) as your location for the other RTL-SDR modes."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR at 2.048 MS/s on 1575.42 MHz (the toolkit tunes it)",
        bandwidth="2 MHz",
        frequencies=(("1575.42 MHz", "GPS L1 C/A"),),
        notes="GPS signals are far below the noise, so the antenna matters most: an active GPS antenna "
              "(with its own LNA, powered by a bias tee or its own supply) with a clear view of the sky. "
              "A passive antenna or the stock whip may get a fix outdoors with patience; indoors, "
              "probably not. Set the frequency correction first (run ADS-B and use its button): GPS "
              "needs it within a few ppm. From cold, a fix takes 1-5 minutes: each satellite's orbit "
              "data takes 30 s to arrive, and 4 satellites are needed.",
    )

    def command(self):
        self._dir = Path(tempfile.mkdtemp(prefix="sdr-toolkit-gps-"))
        (self._dir / "out").mkdir()
        v = self.values
        if self.live():
            dev = self.device_index()
            source = {"live": f"rtl={dev.lstrip(':')}", "gain": self.gain_db()}
        else:
            self._file = self.file_path()
            source = {"stdin": True}
        conf = gnss.config(source, float(v["ppm"]), str(self._dir / "out"), channels=int(v["channels"]),
                           doppler=int(v["doppler"]) * 1000)
        (self._dir / "gps.conf").write_text(conf)
        self._rx = gnss.Receiver()
        self._fixes: list[tuple] = []
        self._saved = None
        self._fed_at = None
        self._last_table = 0.0
        return ["gnss-sdr", f"--log_dir={self._dir}", f"--config_file={self._dir / 'gps.conf'}"]

    def workdir(self):
        return str(self._dir)

    def feeder(self):
        if self.live():
            return None
        return self._feed()

    def _feed(self):
        with open(self._file, "rb") as f:
            while chunk := f.read(1 << 20):
                yield (np.frombuffer(chunk, np.uint8) ^ 0x80).tobytes()     # unsigned -> signed bytes
        self._fed_at = time.time()

    def listening_on(self):
        return "1575.42 MHz"

    def parse(self, line):
        line = ANSI.sub("", line)
        if "no supported devices" in line.lower() or "usb_claim_interface" in line.lower():
            return self.device_problem(line) or []
        if "Unable to connect flowgraph" in line or "configuration file is not well defined" in line:
            return [Event("line", f"GNSS-SDR: {line.strip()} (see Raw data)", style="warn")]
        kind = self._rx.feed(line)
        events = []
        if kind == "fix":
            lat, lon, h, used, utc = self._rx.fix
            self._fixes = (self._fixes + [(lat, lon, h)])[-30:]
            if self._rx.fixes == 1:
                events.append(Event("line", f"First fix: {lat:.5f}, {lon:.5f}, {h:.0f} m ({utc} UTC)"))
                events.append(Event("log", f"GPS fix {lat:.5f}, {lon:.5f}, height {h:.0f} m"))
            events += self._maybe_save()
        elif kind == "track" and len(self._rx.sats) == 1:
            events.append(Event("line", "Found the first satellite; waiting for orbit data (about 30 s each)"))
        if kind or time.time() - self._last_table > 1:
            events += self._tables()
        return events

    def average(self):
        if not self._fixes:
            return None
        lat = sum(f[0] for f in self._fixes) / len(self._fixes)
        lon = sum(f[1] for f in self._fixes) / len(self._fixes)
        return lat, lon

    def _maybe_save(self):
        """Save the averaged fix as your location, after a few fixes, and again when it moves."""
        avg = self.average()
        if not self.values["fill"] or len(self._fixes) < 5 or avg is None:
            return []
        if self._saved is not None and _moved_m(avg, self._saved) < 30:
            return []
        self._saved = avg
        lat, lon = f"{avg[0]:.5f}", f"{avg[1]:.5f}"
        self.values["lat"], self.values["lon"] = lat, lon
        return [Event("setting", data={"lat": lat, "lon": lon}),
                Event("status", f"Your location set to {lat}, {lon} (average of {len(self._fixes)} fixes)")]

    def _tables(self):
        self._last_table = time.time()
        rows = []
        for s in sorted(self._rx.sats.values(), key=lambda s: (not s.tracking, s.prn)):
            rows.append({"prn": f"PRN {s.prn:02d}", "channel": s.channel if s.tracking else "",
                         "cn0": f"{s.cn0:.0f}" if s.cn0 is not None else "",
                         "nav": ("unhealthy, not used" if not s.healthy else
                                 "complete" if s.subframes >= 3 else f"{s.subframes} of 3 parts"),
                         "state": "tracking" if s.tracking else "lost"})
        events = [Event("table", data={"rows": rows})]
        if self._fixes:
            events.append(Event("points", data={"points": [], "home": self.average()}))
        return events

    def activity(self):
        tracked = self._rx.tracking() if hasattr(self, "_rx") else []
        ready = sum(1 for s in tracked if s.subframes >= 3 and s.healthy)
        parts = [super().activity(), f"tracking {len(tracked)} satellites, {ready} with orbit data"]
        if self._rx.fix:
            lat, lon, h, used, _ = self._rx.fix
            parts.append(f"fix {lat:.5f}, {lon:.5f}, {h:.0f} m" + (f" from {used}" if used else ""))
        elif self.live() and int(self.values["ppm"]) == 0 and int(self.values["doppler"]) <= 20:
            parts.append("frequency correction is 0: set it first (ADS-B's button) or widen the search")
        return " · ".join(parts)

    def poll(self):
        # GNSS-SDR keeps waiting at the end of its input: stop it once the recording is in.
        if (not self.live() and self._fed_at and time.time() - self._fed_at > 5 and self._proc
                and self._proc.poll() is None):
            self._ended = True
            self._proc.terminate()
        return super().poll()

    def finished(self, code):
        if getattr(self, "_ended", False):
            fix = "no fix" if not self._rx.fix else f"last fix {self._rx.fix[0]:.5f}, {self._rx.fix[1]:.5f}"
            return [Event("status", f"End of the recording ({fix})"),
                    Event("line", f"End of the recording ({fix})", style="dim")]
        return super().finished(code)

    def start(self, values):
        self._ended = False
        super().start(values)

    def stop(self):
        events = super().stop()
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)
        return events
