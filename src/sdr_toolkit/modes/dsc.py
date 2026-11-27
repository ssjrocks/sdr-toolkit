"""Marine DSC on VHF channel 70 (156.525 MHz): distress alerts with the ship's position and what's wrong,
urgency and safety calls, and the routine calls between ships and coast stations. Decoded by the toolkit's own
ITU-R M.493 decoder (engines/dsc.py) from rtl_fm's FM audio."""

import json
import time

from .. import system
from .base import Event, Setting, Tuning
from .radio import LOCATION_SETTINGS, RADIO_SETTINGS, RadioMode, fm_audio_chunks, home

AUDIO_RATE = 48000
CHANNEL_70 = "156.525M"


class DSCMode(RadioMode):
    id = "dsc"
    name = "Marine DSC (VHF channel 70)"
    group = "SDR"
    summary = ("Digital selective calls on marine VHF channel 70: distress alerts (who, where and what's "
               "wrong), urgency and safety calls, and the routine calls ships and coast stations make to each "
               "other. Distress positions go on the map.")
    output = "table"
    requires = ("rtl-sdr",)
    map = True
    key_column = "mmsi"
    columns = (("mmsi", "MMSI"), ("kind", "Station"), ("category", "Category"), ("last_call", "Latest call"),
               ("count", "Calls"), ("last", "Last heard"))
    iq_rate = 240000
    settings = RADIO_SETTINGS + LOCATION_SETTINGS + (
        Setting("freq", "Frequency", CHANNEL_70, "text", restart=True,
                help="Channel 70 (156.525 MHz) is DSC's only VHF channel."),
        Setting("file_rate", "Recording sample rate", 240000, "int", 48000, 3200000, 1000, suffix=" Hz",
                restart=True),
        Setting("file_offset", "Channel offset in recording", 0, "int", -1600000, 1600000, 1000, suffix=" Hz",
                restart=True, help="How far channel 70 sits from the recording's centre frequency."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR, FM-demodulated by rtl_fm (the toolkit tunes it)",
        bandwidth="25 kHz channel",
        frequencies=(("156.525 MHz", "VHF channel 70: DSC"),),
        notes="Calls are short (half a second) and occasional: coast stations' tests and routine calls, ships "
              "calling each other, and now and then a distress or urgency alert and its relays. Heard to about "
              "the same range as AIS: a marine or 2 m antenna near water.",
    )

    def listening_on(self):
        return str(self.values["freq"]).rstrip("M") + " MHz"

    def upstream(self):
        if not self.live():
            return None
        v = self.values
        f = float(str(v["freq"]).rstrip("M")) * 1e6
        if self.bridge():
            return self.relay_stream(f + 60000, 240000, fm=AUDIO_RATE, channel=25000, offset=-60000)
        cmd = ["rtl_fm", "-d", self.device_index().lstrip(":"), "-f", str(int(f)), "-M", "fm",
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
        self._stations: dict[str, dict] = {}
        self._calls = 0
        self._dirty = False
        self._last_table = 0.0
        return system.python_command("sdr_toolkit.engines.dsc") + ["--rate", str(AUDIO_RATE)]

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            return problem if problem is not None else []
        try:
            c = json.loads(line)
        except ValueError:
            return []
        if c.get("event") != "call":
            return []
        self._calls += 1
        key = c.get("from") or "?"
        now = time.time()
        s = self._stations.setdefault(key, {"count": 0, "calls": [], "first": now})
        s["count"] += 1
        s["last"] = now
        s["seq"] = self._calls
        s["kind"] = c.get("from_kind", "")
        s["calls"] = (s["calls"] + [(now, c)])[-20:]
        if "lat" in c:
            s["pos"] = (c["lat"], c["lon"])
        self._dirty = True
        text = c["summary"] + ("" if c.get("ecc_ok") else " (error check failed)")
        events = [Event("log", text)]
        distress = c.get("format_code") == 112 or c.get("category") == "Distress"
        if distress or c.get("category") == "Urgency":
            events.append(Event("line", text, style="warn"))
        return events

    def poll(self):
        events = super().poll()
        if getattr(self, "_dirty", False) and time.time() - self._last_table >= 1.0:
            self._dirty = False
            self._last_table = time.time()
            events += self._views()
        return events

    def stop(self):
        events = super().stop()
        if getattr(self, "_stations", None):
            events += self._views()
        return events

    def _views(self):
        rows, points = [], []
        for key, s in sorted(self._stations.items(), key=lambda kv: (-kv[1]["last"], -kv[1]["seq"])):
            c = s["calls"][-1][1]
            cat = "Distress" if c.get("format_code") == 112 else c.get("category", c.get("format", ""))
            rows.append({"mmsi": key, "kind": s["kind"], "category": cat, "last_call": c["summary"],
                         "count": s["count"], "last": time.strftime("%H:%M:%S", time.localtime(s["last"]))})
            if "pos" in s:
                points.append({"id": key, "lat": s["pos"][0], "lon": s["pos"][1], "kind": "ship",
                               "label": f"{key} ({cat})"})
        return [Event("table", data={"rows": rows}), Event("points", data={"points": points, "home": home(self.values)})]

    def details(self, key):
        s = getattr(self, "_stations", {}).get(key)
        if s is None:
            return None
        c = s["calls"][-1][1]
        f = [("MMSI", key), ("Station", s["kind"]), ("Calls heard", str(s["count"]))]
        names = {"format": "Format", "category": "Category", "to": "To", "nature": "Nature of distress",
                 "distress_id": "Station in distress", "time": "Position time", "tc1": "Telecommand",
                 "tc2": "Telecommand 2", "channel": "Working channel", "channel_tx": "Its transmit channel",
                 "subsequent": "Distress traffic by", "eos": "Ends with", "area": "Area"}
        for k, label in names.items():
            if c.get(k):
                f.append((label, str(c[k])))
        if "lat" in c:
            f.append(("Position", f"{c['lat']:.4f}, {c['lon']:.4f}"))
        f.append(("Error check", "passed" if c.get("ecc_ok") else "failed"))
        for t, old in s["calls"][-6:][::-1]:
            f.append((time.strftime("%H:%M:%S", time.localtime(t)), old["summary"]))
        return {"title": key, "subtitle": s["kind"], "fields": f,
                "links": [("Look up the MMSI", f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{key}")]
                if s["kind"] == "ship" else []}

    def activity(self):
        return super().activity() + f" · {len(getattr(self, '_stations', {}))} stations, {getattr(self, '_calls', 0)} calls"
