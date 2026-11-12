"""Radiosondes: weather balloons' position, altitude, climb and weather readings, on a map.

The engine (engines/sonde_rx.py) finds the sondes in the dongle's band and decodes each one with
rs1729's decoders (RS41, DFM, M10/M20, RS92, iMet-54...). Each sonde is a row and a marker with its
track; once it's coming down, a landing point is estimated from its descent rate, speed and
heading (a straight line on the wind it has now: rough, it changes as it falls).
"""

import json
import time

from .. import system
from ..coverage import bearing_km, destination
from .base import Event, MissingParts, Setting, Tuning
from .radio import LOCATION_SETTINGS, RADIO_SETTINGS, RadioMode, home

COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def landing(s, ground_m=0.0):
    """Where and when a sonde coming down lands, at its descent rate and on its wind now: (lat, lon,
    seconds from now), or None while it's going up or nearly level."""
    v = s.get("vel_v")
    if v is None or v > -1.0 or s.get("lat") is None or s.get("alt") is None:
        return None
    seconds = max(float(s["alt"]) - ground_m, 0.0) / -float(v)
    km = float(s.get("vel_h") or 0.0) * seconds / 1000
    lat, lon = destination((s["lat"], s["lon"]), float(s.get("heading") or 0.0), km)
    return lat, lon, seconds


def compass(deg) -> str:
    return COMPASS[int((deg % 360) / 45 + 0.5) % 8]


class SondeMode(RadioMode):
    id = "sondes"
    name = "Radiosondes (weather balloons)"
    group = "RTL-SDR"
    summary = ("Weather balloons' radiosondes: position, altitude, climb, temperature, humidity and pressure, "
               "on a map with their tracks and where they'll land. Decoded with rs1729's decoders.")
    output = "table"
    map = True
    key_column = "id"
    requires = ("radiosonde", "rtl-sdr")
    columns = (("id", "Serial"), ("type", "Type"), ("freq", "MHz"), ("alt", "Altitude m"), ("climb", "Climb m/s"),
               ("speed", "Speed km/h"), ("heading", "Heading°"), ("temp", "Temp °C"), ("humidity", "Humidity %"),
               ("pressure", "hPa"), ("batt", "Battery V"), ("sats", "Sats"), ("dist", "Distance"),
               ("lands", "Lands"), ("frames", "Frames"), ("seen", "Seen"))
    settings = RADIO_SETTINGS + LOCATION_SETTINGS + (
        Setting("centre_mhz", "Centre frequency", 402.0, "float", 400.0, 406.0, 0.1, 3, suffix=" MHz", restart=True,
                help="The dongle watches the band around this (2.4 MHz at the default rate). Sondes are on "
                     "400-406 MHz; in Australia the Bureau of Meteorology's are around 400.5-403.5 MHz."),
        Setting("freqs", "Frequencies", "", "text", restart=True,
                help="Empty: sondes are found by themselves (any narrow signal well above the noise is tried). "
                     "Or MHz to decode, separated by commas, e.g. 401.5, 402.0."),
        Setting("rate", "Sample rate", 2400000, "choice", restart=True,
                choices=((2400000, "2.4 MS/s (2.2 MHz of band)"), (1920000, "1.92 MS/s"), (1440000, "1.44 MS/s"),
                         (960000, "0.96 MS/s"))),
        Setting("ground_m", "Ground height", 0, "int", -100, 5000, 10, suffix=" m",
                help="For the landing estimate: about the height of the land where it's coming down."),
        Setting("ttl", "Drop sondes after", 30, "int", 1, 240, 1, suffix=" min",
                help="Sondes not heard for this long leave the list and the map."),
        Setting("file_rate", "Recording sample rate", 2400000, "int", 48000, 3200000, 48000, suffix=" Hz",
                restart=True, help="A multiple of 48 kHz."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR around 402 MHz; the sondes in it are found and decoded by themselves",
        bandwidth="Each sonde is 5-15 kHz wide; the dongle watches 2.2 MHz of the 400-406 MHz band",
        frequencies=(("400.5-403.5 MHz", "Australia: Bureau of Meteorology's Vaisala RS41s"),
                     ("400-406 MHz", "radiosondes everywhere (RS41, DFM, M10/M20, iMet, Meisei...)")),
        notes="Weather services launch them on a schedule, often twice a day (in Australia around 9 am and "
              "9 pm local time, at the Bureau's stations); they climb to 30 km or so over 1.5-2 hours, burst "
              "and come down on a parachute. Heard from far away while high; a vertical antenna for 400 MHz "
              "helps. The landing estimate is a straight line on the current wind: SondeHub's predictor "
              "(linked in Details) uses wind forecasts.",
    )

    def upstream(self):
        if not self.live():
            return None
        v = self.values
        freq, rate = float(v["centre_mhz"]) * 1e6, int(v["rate"])
        if self.bridge():
            return self.relay_stream(freq, rate)
        cmd = ["rtl_sdr", "-d", self.device_index().lstrip(":"), "-f", str(int(freq)), "-s", str(rate),
               "-p", str(v["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def command(self):
        from ..engines.sonde_rx import program
        if program("rs41mod") is None or program("dft_detect") is None:
            raise MissingParts(["radiosonde"])
        v = self.values
        live = self.live()
        rate = int(v["rate"]) if live else int(v["file_rate"])
        if rate % 48000:
            raise RuntimeError("The sample rate must be a multiple of 48 kHz (2.4 MS/s, 1.92, 1.44, 0.96...).")
        freqs = []
        for f in str(v["freqs"]).replace(";", ",").split(","):
            if f.strip():
                try:
                    freqs.append(float(f) * 1e6)
                except ValueError:
                    raise RuntimeError(f"Frequencies: {f.strip()!r} isn't a number in MHz.") from None
        cmd = system.python_command("sdr_toolkit.engines.sonde_rx") + [
            "--input", "-" if live else self.file_path(), "--rate", str(rate),
            "--centre", str(int(float(v["centre_mhz"]) * 1e6))]
        if freqs:
            cmd += ["--freqs", ",".join(str(int(f)) for f in freqs)]
        self._sondes: dict[str, dict] = {}
        self._signals: dict[float, str] = {}
        self._last_table = 0.0
        self._dirty = False
        return cmd

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            return problem if problem is not None else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        mhz = f"{e.get('freq', 0) / 1e6:.3f}"
        if e.get("event") == "channel":
            state, kind = e.get("state"), e.get("type") or ""
            self._signals[e.get("freq", 0)] = state
            text = {"detecting": f"Signal on {mhz} MHz: identifying it",
                    "decoding": f"{mhz} MHz: {kind}, decoding",
                    "lost": f"{mhz} MHz: {kind or 'the sonde'} not heard for 2 min",
                    "unknown": f"{mhz} MHz: not a sonde the decoders know" + (f" ({e['error']})" if e.get("error") else "")}
            return [Event("log", text.get(state, f"{mhz} MHz: {state}"), style="" if state == "decoding" else "dim"),
                    Event("status", self.activity())]
        if e.get("event") != "frame" or not e.get("id"):
            return []
        sid = str(e["id"])
        s = self._sondes.get(sid)
        new = s is None
        if new:
            s = self._sondes[sid] = {"id": sid, "track": [], "frames": 0, "first": time.time()}
        s.update({k: e[k] for k in ("type", "subtype", "lat", "lon", "alt", "vel_h", "vel_v", "heading", "sats",
                                    "batt", "temp", "humidity", "pressure", "frame", "datetime") if k in e})
        s["freq"], s["last"] = e.get("freq"), time.time()
        s["frames"] += 1
        if s.get("lat") is not None and (not s["track"] or s["track"][-1] != (s["lat"], s["lon"])):
            s["track"] = (s["track"] + [(s["lat"], s["lon"])])[-2000:]
            s["pos_t"] = time.time()
        self._dirty = True
        if new:
            return [Event("log", f"{sid} ({s.get('subtype') or s.get('type')}) on {mhz} MHz at {s.get('alt', 0):,.0f} m")]
        return []

    def poll(self):
        events = super().poll()
        if getattr(self, "_dirty", False) and time.time() - self._last_table >= 1.0:
            self._dirty = False
            self._last_table = time.time()
            events += self._tables()
        return events

    def finished(self, code):
        """The recording ended (or the engine stopped): show the last updates too."""
        return (self._tables() if getattr(self, "_dirty", False) else []) + super().finished(code)

    def stop(self):
        events = super().stop()
        return events + (self._tables() if getattr(self, "_dirty", False) else [])

    def _tables(self):
        self._dirty = False
        now = time.time()
        ttl = float(self.values["ttl"]) * 60
        self._sondes = {k: s for k, s in self._sondes.items() if now - s["last"] < ttl}
        here = home(self.values)
        ground = float(self.values["ground_m"])
        rows, points = [], []
        for s in sorted(self._sondes.values(), key=lambda s: -s["last"]):
            pos = (s["lat"], s["lon"]) if s.get("lat") is not None else None
            dist = ""
            if here and pos:
                b, km = bearing_km(here, pos)
                dist = f"{km:.1f} km {compass(b)}"
            land = landing(s, ground)
            lands = ""
            if land:
                lands = f"in {land[2] / 60:.0f} min"
                if here:
                    b, km = bearing_km(here, land[:2])
                    lands += f", {km:.1f} km {compass(b)} of you"
            fmt = lambda k, f: f.format(s[k]) if s.get(k) is not None else ""   # noqa: E731
            rows.append({"id": s["id"], "type": s.get("subtype") or s.get("type", ""),
                         "freq": f"{s['freq'] / 1e6:.3f}" if s.get("freq") else "",
                         "alt": fmt("alt", "{:,.0f}"), "climb": fmt("vel_v", "{:+.1f}"),
                         "speed": f"{s['vel_h'] * 3.6:.0f}" if s.get("vel_h") is not None else "",
                         "heading": fmt("heading", "{:.0f}"), "temp": fmt("temp", "{:.1f}"),
                         "humidity": fmt("humidity", "{:.0f}"), "pressure": fmt("pressure", "{:.1f}"),
                         "batt": fmt("batt", "{:.1f}"), "sats": fmt("sats", "{}"), "dist": dist, "lands": lands,
                         "frames": s["frames"], "seen": f"{now - s['last']:.0f} s"})
            if pos:
                alt = s.get("alt")
                points.append({"id": s["id"], "lat": pos[0], "lon": pos[1], "kind": "sonde",
                               "label": f"{s['id']} {alt:,.0f} m" if alt is not None else s["id"],
                               "heading": s.get("heading"), "track": list(s["track"]), "name": s["id"],
                               "alt": alt, "pos_t": s.get("pos_t"), "msg_t": s["last"],
                               "gs": s["vel_h"] * 1.94384 if s.get("vel_h") is not None else None,
                               "course": s.get("heading"),
                               "info": {k: s[k] for k in ("type", "subtype", "freq") if s.get(k)}})
                if land:
                    points.append({"id": f"{s['id']} landing", "lat": land[0], "lon": land[1], "kind": "landing",
                                   "label": f"{s['id']} lands ~{time.strftime('%H:%M', time.localtime(now + land[2]))}",
                                   "track": [pos, land[:2]]})
        return [Event("table", data={"rows": rows}), Event("points", data={"points": points, "home": here})]

    def activity(self):
        found = sum(1 for st in self._signals.values() if st == "decoding") if hasattr(self, "_signals") else 0
        n = len(getattr(self, "_sondes", {}))
        return f"{super().activity()} · {n} sonde{'s' if n != 1 else ''}, {found} being decoded"

    def listening_on(self):
        return f"{float(self.values['centre_mhz']):.1f} MHz ±{int(self.values['rate']) / 2e6 * 0.9:.1f}"

    def details(self, key):
        s = getattr(self, "_sondes", {}).get(key) or next(
            (v for k, v in getattr(self, "_sondes", {}).items() if k.lower() == str(key).lower()), None)
        if s is None:
            return None
        f = []

        def add(label, value):
            if value not in (None, ""):
                f.append((label, str(value)))
        add("Type", s.get("subtype") or s.get("type"))
        add("Frequency", f"{s['freq'] / 1e6:.3f} MHz" if s.get("freq") else None)
        add("Altitude", f"{s['alt']:,.0f} m" if s.get("alt") is not None else None)
        add("Climb", f"{s['vel_v']:+.1f} m/s" if s.get("vel_v") is not None else None)
        add("Speed", f"{s['vel_h'] * 3.6:.0f} km/h towards {s.get('heading', 0):.0f}°" if s.get("vel_h") is not None
            else None)
        add("Temperature", f"{s['temp']:.1f} °C" if s.get("temp") is not None else None)
        add("Humidity", f"{s['humidity']:.0f} %" if s.get("humidity") is not None else None)
        add("Pressure", f"{s['pressure']:.1f} hPa" if s.get("pressure") is not None else None)
        add("Battery", f"{s['batt']:.1f} V" if s.get("batt") is not None else None)
        add("GPS satellites", s.get("sats"))
        add("Sonde's time", s.get("datetime"))
        land = landing(s, float(self.values["ground_m"]))
        if land:
            add("Lands (rough)", f"in {land[2] / 60:.0f} min at {land[0]:.4f}, {land[1]:.4f}")
        add("Frames heard", s.get("frames"))
        return {"title": s["id"], "subtitle": "radiosonde", "fields": f,
                "links": [("SondeHub (its flight, and a landing prediction from wind forecasts)",
                           f"https://sondehub.org/{s['id']}")]}
