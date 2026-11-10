"""AIS ship positions on 161.975 / 162.025 MHz: direwolf receives, the toolkit decodes the sentences."""

import math
import shutil
import tempfile
import time
from pathlib import Path

from ..engines.ais import Assembler
from .base import Event, Setting, Tuning
from .process import looks_like_error
from .radio import LOCATION_SETTINGS, RADIO_SETTINGS, RadioMode, fm_audio_chunks, home

AUDIO_RATE = 48000


def distance_km(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


class AISMode(RadioMode):
    id = "ais"
    name = "AIS ships (VHF)"
    group = "RTL-SDR"
    summary = "Ship names, positions, speed and course from their AIS transponders, on a map. Received by direwolf."
    output = "table"
    map = True
    columns = (("mmsi", "MMSI"), ("name", "Name"), ("type", "Type"), ("status", "Status"), ("pos", "Position"),
               ("speed", "Speed kn"), ("course", "Course°"), ("dist", "Distance"), ("seen", "Seen"))
    requires = ("direwolf", "rtl-sdr")
    key_column = "mmsi"
    iq_rate = 240000
    settings = RADIO_SETTINGS + LOCATION_SETTINGS + (
        Setting("channel", "Channel", "161.975M", "choice", restart=True,
                choices=(("161.975M", "AIS 1 (87B), 161.975 MHz"), ("162.025M", "AIS 2 (88B), 162.025 MHz"),
                         ("custom", "Other (below)")),
                help="Ships alternate between both channels: either one shows them all, more slowly."),
        Setting("custom_freq", "Other frequency", "161.975M", "text", restart=True),
        Setting("ttl", "Drop ships after", 30, "int", 1, 240, suffix=" min",
                help="Ships not heard for this long leave the list and the map."),
        Setting("file_rate", "Recording sample rate", 240000, "int", 48000, 3200000, 1000, suffix=" Hz",
                restart=True),
        Setting("file_offset", "Channel offset in recording", 0, "int", -1600000, 1600000, 1000, suffix=" Hz",
                restart=True, help="How far the AIS channel sits from the recording's centre frequency."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR, FM-demodulated by rtl_fm (the toolkit tunes it)",
        bandwidth="25 kHz channel",
        frequencies=(("161.975 MHz", "AIS 1"), ("162.025 MHz", "AIS 2")),
        notes="Ships are heard to about 30-60 km from a high antenna near water; inland you may only "
              "hear ships on rivers. A VHF marine or 2 m antenna works well. Set the dongle's frequency "
              "correction (ppm): a 25 kHz channel doesn't leave much room.",
    )

    def freq(self):
        return self.values["custom_freq"] if self.values["channel"] == "custom" else self.values["channel"]

    def listening_on(self):
        return self.freq().rstrip("M") + " MHz"

    def upstream(self):
        if not self.live():
            return None
        v = self.values
        if self.bridge():
            # 240 kS/s, tuned 60 kHz above the channel to keep it off the dongle's DC spike.
            f = float(self.freq().rstrip("M")) * 1e6
            return self.relay_stream(f + 60000, 240000, fm=AUDIO_RATE, channel=25000, offset=-60000)
        cmd = ["rtl_fm", "-d", self.device_index().lstrip(":"), "-f", self.freq(), "-M", "fm",
               "-s", str(AUDIO_RATE), "-p", str(v["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def feeder(self):
        if self.live():
            return None
        return fm_audio_chunks(self.file_path(), self.values["file_rate"], self.values["file_offset"],
                               AUDIO_RATE, 25000)

    def command(self):
        self._dir = Path(tempfile.mkdtemp(prefix="sdr-toolkit-ais-"))
        (self._dir / "direwolf.conf").write_text(
            f"ADEVICE stdin null\nARATE {AUDIO_RATE}\nACHANNELS 1\nMODEM AIS\nAGWPORT 0\nKISSPORT 0\n")
        self._ready = False
        self._asm = Assembler()
        self._ships: dict[int, dict] = {}
        self._messages = 0
        self._last_table = 0.0
        self._dirty = False
        return ["direwolf", "-c", "direwolf.conf", "-r", str(AUDIO_RATE), "-n", "1", "-b", "16", "-t", "0",
                "-B", "AIS", "-"]

    def workdir(self):
        return str(self._dir)

    def parse(self, line):
        line = line.strip()
        if not self._ready:
            if line.startswith("Disabled AGW") or line.startswith("Disabled KISS"):
                self._ready = True
            return [Event("line", line, style="warn")] if looks_like_error(line) else []
        if "!AIVDM" not in line:
            return []
        report = self._asm.add(line[line.index("!AIVDM"):])
        if report is None or not report.fields:
            return []
        self._messages += 1
        now = time.time()
        ship = self._ships.setdefault(report.mmsi, {"mmsi": report.mmsi, "track": []})
        ship.update(report.fields)
        ship["last"] = now
        if "lat" in report.fields:
            ship["track"] = (ship["track"] + [(report.fields["lat"], report.fields["lon"])])[-30:]
        self._dirty = True
        return [Event("log", self._describe(report), style="pos" if "lat" in report.fields else "")]

    def _describe(self, report) -> str:
        s = self._ships[report.mmsi]
        f = report.fields
        name = s.get("name") or str(report.mmsi)
        if "lat" in f:
            text = f"{name}: {f['lat']:.5f}, {f['lon']:.5f}"
            if "speed" in f:
                text += f", {f['speed']:.1f} kn"
            if "course" in f:
                text += f", course {f['course']:.0f}°"
            return text
        bits = [f"{k} {f[k]}" for k in ("callsign", "type", "destination") if k in f]
        return f"{report.mmsi} is {f.get('name', name)}" + (f" ({', '.join(bits)})" if bits else "")

    def poll(self):
        events = super().poll()
        if self._dirty and time.time() - self._last_table >= 1.0:
            self._dirty = False
            self._last_table = time.time()
            events += self._tables()
        return events

    def _tables(self):
        now = time.time()
        ttl = self.values["ttl"] * 60
        self._ships = {k: s for k, s in self._ships.items() if now - s["last"] < ttl}
        here = home(self.values)
        rows, points = [], []
        for s in sorted(self._ships.values(), key=lambda s: -s["last"]):
            pos = (s["lat"], s["lon"]) if "lat" in s else None
            dist = f"{distance_km(here, pos):.1f} km" if here and pos else ""
            rows.append({"mmsi": s["mmsi"], "name": s.get("name", ""), "type": s.get("type", ""),
                         "status": s.get("status", ""),
                         "pos": f"{pos[0]:.5f}, {pos[1]:.5f}" if pos else "",
                         "speed": f"{s['speed']:.1f}" if "speed" in s else "",
                         "course": f"{s['course']:.0f}" if "course" in s else "", "dist": dist,
                         "seen": f"{int(now - s['last'])} s"})
            if pos:
                points.append({"id": s["mmsi"], "lat": pos[0], "lon": pos[1], "kind": "ship",
                               "label": s.get("name") or str(s["mmsi"]),
                               "heading": s.get("heading", s.get("course")), "track": s["track"],
                               "name": s.get("name", ""),
                               "info": {k: s[k] for k in ("name", "callsign", "imo", "type", "destination",
                                                          "length", "beam", "class") if k in s}})
        return [Event("table", data={"rows": rows}),
                Event("points", data={"points": points, "home": here})]

    def activity(self):
        return f"{super().activity()} · {len(self._ships)} ships, {self._messages} messages"

    def details(self, key):
        try:
            s = self._ships.get(int(key))
        except (ValueError, AttributeError):
            return None
        if s is None:
            return None
        f = []

        def add(label, value):
            if value not in (None, ""):
                f.append((label, str(value)))

        add("MMSI", s["mmsi"])
        add("Name", s.get("name"))
        add("Callsign", s.get("callsign"))
        add("IMO number", s.get("imo"))
        add("Type", s.get("type"))
        add("AIS class", s.get("class"))
        add("Status", s.get("status"))
        add("Destination", s.get("destination"))
        add("ETA", s.get("eta"))
        if "length" in s:
            add("Size", f"{s['length']} × {s['beam']} m")
        add("Draught", f"{s['draught']:.1f} m" if "draught" in s else None)
        add("Speed", f"{s['speed']:.1f} kn" if "speed" in s else None)
        add("Course", f"{s['course']:.0f}°" if "course" in s else None)
        add("Heading", f"{s['heading']}°" if "heading" in s else None)
        if "lat" in s:
            add("Position", f"{s['lat']:.5f}, {s['lon']:.5f}")
            here = home(self.values)
            if here:
                add("Distance", f"{distance_km(here, (s['lat'], s['lon'])):.1f} km")
        add("Last heard", f"{time.time() - s['last']:.0f} s ago")
        links = [("MarineTraffic", f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{s['mmsi']}"),
                 ("VesselFinder", f"https://www.vesselfinder.com/vessels/details/{s['mmsi']}")]
        return {"title": s.get("name") or str(s["mmsi"]),
                "subtitle": " · ".join(x for x in (s.get("type", ""), s.get("callsign", "")) if x),
                "fields": f, "photo": {"kind": "ship", "type": s.get("type", "")}, "links": links}

    def stop(self):
        events = super().stop() + (self._tables() if hasattr(self, "_ships") else [])
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)
        return events

    def finished(self, code):
        if not self.live():
            return self._tables() + [Event("status", "recording finished")]
        return super().finished(code)
