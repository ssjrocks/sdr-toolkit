"""ADS-B / Mode S aircraft on 1090 MHz, decoded by readsb."""

import json
import shutil
import tempfile
import time
from pathlib import Path

from .base import Event, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode


class ADSBMode(RadioMode):
    id = "adsb"
    name = "ADS-B aircraft (1090 MHz)"
    group = "RTL-SDR"
    summary = "Aircraft positions, callsigns, altitude and speed from their 1090 MHz transponders. Decoded by readsb."
    output = "table"
    columns = (("hex", "ICAO"), ("flight", "Callsign"), ("squawk", "Squawk"), ("alt", "Altitude ft"),
               ("gs", "Speed kt"), ("track", "Track°"), ("pos", "Position"), ("dist", "Distance"),
               ("rssi", "dBFS"), ("msgs", "Msgs"), ("seen", "Seen"))
    requires = ("readsb",)
    iq_rate = 2400000
    settings = RADIO_SETTINGS + (
        Setting("lat", "Your latitude", "", "text", restart=True,
                help="Optional, decimal degrees (e.g. -33.87). Gives distances, and positions from "
                     "single messages."),
        Setting("lon", "Your longitude", "", "text", restart=True),
        Setting("units", "Distance in", "km", "choice", choices=(("km", "km"), ("nm", "nautical miles"))),
        Setting("ttl", "Drop aircraft after", 60, "int", 10, 600, 10, suffix=" s",
                help="Aircraft not heard for this long leave the list."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR at 2.4 MS/s on 1090 MHz (the toolkit tunes it)",
        bandwidth="2.4 MHz",
        frequencies=(("1090 MHz", "ADS-B and Mode S, worldwide"),),
        notes="1090 MHz is line of sight: an antenna high up, outside or at a window, makes the most "
              "difference. The stock whip works for aircraft within 20-50 km; a 1090 MHz antenna "
              "reaches 200+ km. Leave gain on auto at first.",
    )

    def command(self):
        self._dir = Path(tempfile.mkdtemp(prefix="sdr-toolkit-adsb-"))
        v = self.values
        if self.live():
            cmd = ["readsb", "--device-type", "rtlsdr", f"--device={self.device_index().lstrip(':')}",
                   f"--ppm={v['ppm']}"]
            if self.gain_db():
                cmd.append(f"--gain={self.gain_db()}")
        else:
            cmd = ["readsb", "--device-type", "ifile", f"--ifile={self.file_path()}", "--iformat", "UC8",
                   "--throttle"]
        cmd += ["--quiet", f"--write-json={self._dir}", "--write-json-every=1"]
        try:
            lat, lon = float(v["lat"]), float(v["lon"])
            cmd += [f"--lat={lat}", f"--lon={lon}"]
        except ValueError:
            pass
        self._last_read = 0.0
        self._known = {}
        return cmd

    def parse(self, line):
        problem = self.device_problem(line)
        if problem is not None:
            return problem
        if self._problem:
            return []          # readsb's follow-up lines ("sdrOpen() failed, exiting!")
        if any(w in line for w in ("rror", "FATAL", "Failed", "failed")):
            return [Event("line", line, style="warn"), Event("status", line)]
        return []

    def poll(self):
        events = super().poll()
        if time.time() - self._last_read >= 1.0:
            self._last_read = time.time()
            events += self._read_json()
        return events

    def _read_json(self):
        try:
            data = json.loads((self._dir / "aircraft.json").read_text())
        except (OSError, ValueError):
            return []
        now = data.get("now", time.time())
        for a in data.get("aircraft", []):
            self._known[a["hex"]] = (now - a.get("seen", 0), a)
        ttl = self.values["ttl"]
        self._known = {k: v for k, v in self._known.items() if now - v[0] < ttl}
        rows = []
        for hexid, (last, a) in sorted(self._known.items(), key=lambda kv: kv[1][1].get("r_dst", 1e9)):
            pos = f"{a['lat']:.4f}, {a['lon']:.4f}" if "lat" in a else ""
            dist = ""
            if "r_dst" in a:      # from your position, in nautical miles; r_dir is the bearing
                d = a["r_dst"] * (1.852 if self.values["units"] == "km" else 1)
                dist = f"{d:.1f} {self.values['units']}"
                if "r_dir" in a:
                    dist += f" at {a['r_dir']:.0f}°"
            alt = a.get("alt_baro", a.get("alt_geom", ""))
            track = a.get("track", a.get("calc_track"))
            rows.append({"hex": hexid.upper(), "flight": a.get("flight", "").strip(), "squawk": a.get("squawk", ""),
                         "alt": alt, "gs": round(a["gs"]) if "gs" in a else "",
                         "track": round(track) if track is not None else "", "pos": pos, "dist": dist,
                         "rssi": a.get("rssi", ""), "msgs": a.get("messages", ""),
                         "seen": f"{now - last:.0f} s"})
        return [Event("table", data={"rows": rows}),
                Event("status", f"{len(rows)} aircraft, {data.get('messages', 0)} messages")]

    def stop(self):
        events = super().stop()
        events += self._read_json()
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)
        return events

    def finished(self, code):
        if not self.live():
            return self._read_json() + [Event("status", "recording finished")]
        return super().finished(code)
