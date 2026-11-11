"""ADS-B / Mode S aircraft on 1090 MHz, decoded by readsb."""

import json
import shutil
import tempfile
import time
from pathlib import Path

from .. import aircraftdb
from .base import Event, Setting, Tuning
from .process import looks_like_error
from .radio import LOCATION_SETTINGS, RADIO_SETTINGS, RadioMode


class ADSBMode(RadioMode):
    id = "adsb"
    name = "ADS-B aircraft (1090 MHz)"
    group = "RTL-SDR"
    summary = "Aircraft positions, callsigns, altitude and speed from their 1090 MHz transponders. Decoded by readsb."
    output = "table"
    map = True
    columns = (("hex", "ICAO"), ("flight", "Callsign"), ("squawk", "Squawk"), ("alt", "Altitude ft"),
               ("gs", "Speed kt"), ("track", "Track°"), ("pos", "Position"), ("dist", "Distance"),
               ("rssi", "dBFS"), ("msgs", "Msgs"), ("seen", "Seen"))
    requires = ("readsb",)
    key_column = "hex"
    iq_rate = 2400000
    settings = RADIO_SETTINGS + LOCATION_SETTINGS + (
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
        if self.bridge():
            cmd = ["readsb", "--device-type", "ifile", "--ifile=-", "--iformat", "UC8"]      # relay_stream's output
        elif self.live():
            cmd = ["readsb", "--device-type", "rtlsdr", f"--device={self.device_index().lstrip(':')}",
                   f"--ppm={v['ppm']}"]
            if self.gain_db():
                cmd.append(f"--gain={self.gain_db()}")
        else:
            cmd = ["readsb", "--device-type", "ifile", f"--ifile={self.file_path()}", "--iformat", "UC8",
                   "--throttle"]
        cmd += ["--quiet", f"--write-json={self._dir}", "--write-json-every=1"]
        if aircraftdb.present():          # registrations, types and operators in aircraft.json
            cmd += [f"--db-file={aircraftdb.path()}", "--db-file-lt"]
        try:
            lat, lon = float(v["lat"]), float(v["lon"])
            cmd += [f"--lat={lat}", f"--lon={lon}"]
        except ValueError:
            pass
        self._last_read = 0.0
        self._known = {}
        self._tracks: dict[str, list] = {}
        self._pos_t: dict[str, float] = {}
        self._ppm_readings: list[float] = []     # readsb's 30-second clock measurements
        self._ppm_last = None
        self._logged: dict[str, tuple] = {}
        return cmd

    def parse(self, line):
        problem = self.device_problem(line)
        if problem is not None:
            return problem
        if self._problem:
            return []          # readsb's follow-up lines ("sdrOpen() failed, exiting!")
        if looks_like_error(line):
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
            if "lat" in a and "seen_pos" in a:
                self._pos_t[a["hex"]] = now - a["seen_pos"]      # when that position was heard
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
        self._count = (len(rows), data.get("messages", 0))
        points, log = [], []
        for hexid, (last, a) in self._known.items():
            if "lat" not in a:
                continue
            track = self._tracks.setdefault(hexid, [])
            if not track or track[-1] != (a["lat"], a["lon"]):
                track.append((a["lat"], a["lon"]))
                del track[:-60]
            flight = a.get("flight", "").strip()
            alt = a.get("alt_baro", a.get("alt_geom", ""))
            points.append({"id": hexid, "lat": a["lat"], "lon": a["lon"], "kind": "aircraft",
                           "pos_t": self._pos_t.get(hexid, last), "msg_t": last, "gs": a.get("gs"),
                           "course": a.get("track", a.get("calc_track")),
                           "label": f"{flight or hexid.upper()} {alt}" if alt != "" else (flight or hexid.upper()),
                           "heading": a.get("track", a.get("calc_track")), "track": list(track),
                           "name": flight or a.get("r", ""), "alt": alt if isinstance(alt, (int, float)) else None,
                           "info": {k: a[k] for k in ("flight", "r", "t", "desc", "ownOp", "year", "squawk",
                                                      "category") if a.get(k) not in (None, "")}})
            # The decoded log gets a line when an aircraft appears or its callsign arrives.
            key = (flight,)
            if self._logged.get(hexid) != key:
                self._logged[hexid] = key
                log.append(Event("log", f"{hexid.upper()} {flight or '(no callsign yet)'}: {alt} ft, "
                                        f"{a['lat']:.4f}, {a['lon']:.4f}"))
        self._tracks = {k: v for k, v in self._tracks.items() if k in self._known}
        self._pos_t = {k: v for k, v in self._pos_t.items() if k in self._known}
        here = None
        try:
            here = (float(self.values["lat"]), float(self.values["lon"]))
        except (KeyError, ValueError):
            pass
        return [Event("table", data={"rows": rows}),
                Event("points", data={"points": points, "home": here,
                                      # every aircraft's latest message, positions or not (the rings' estimate)
                                      "heard": {h: last for h, (last, _) in self._known.items()}})] + log

    def listening_on(self):
        return "1090 MHz"

    def details(self, key):
        key = key.lower()
        if key not in getattr(self, "_known", {}):
            return None
        last, a = self._known[key]
        flight, reg = a.get("flight", "").strip(), a.get("r", "")
        units = self.values["units"]
        f = []

        def add(label, value):
            if value not in (None, ""):
                f.append((label, str(value)))

        add("ICAO address", key.upper())
        add("Callsign", flight)
        add("Registration", reg)
        add("Type", " · ".join(x for x in (a.get("desc", ""), a.get("t", "")) if x))
        add("Operator / owner", a.get("ownOp"))
        add("Built", a.get("year"))
        add("Squawk", a.get("squawk"))
        if a.get("emergency") not in (None, "", "none"):
            add("Emergency", a["emergency"])
        alt = a.get("alt_baro", a.get("alt_geom"))
        add("Altitude", f"{alt:,} ft" if isinstance(alt, (int, float)) else alt)
        rate = a.get("baro_rate", a.get("geom_rate"))
        add("Climb / descent", f"{rate:+,} ft/min" if isinstance(rate, (int, float)) else None)
        add("Ground speed", f"{a['gs']:.0f} kt" if "gs" in a else None)
        track = a.get("track", a.get("calc_track"))
        add("Track", f"{track:.0f}°" if track is not None else None)
        add("Position", f"{a['lat']:.5f}, {a['lon']:.5f}" if "lat" in a else None)
        if "r_dst" in a:
            d = a["r_dst"] * (1.852 if units == "km" else 1)
            add("Distance", f"{d:.1f} {units}" + (f" at {a['r_dir']:.0f}°" if "r_dir" in a else ""))
        add("Signal", f"{a['rssi']} dBFS" if "rssi" in a else None)
        add("Messages", a.get("messages"))
        add("Category", a.get("category"))
        add("Last heard", f"{time.time() - last:.0f} s ago" if self.live() else None)
        links = [("ADS-B Exchange", f"https://globe.adsbexchange.com/?icao={key}")]
        if flight:
            links.append(("FlightAware", f"https://www.flightaware.com/live/flight/{flight}"))
        return {"title": flight or reg or key.upper(),
                "subtitle": " · ".join(x for x in (reg if flight else "", a.get("desc") or a.get("t", "")) if x),
                "fields": f,
                "photo": {"kind": "aircraft", "hex": key, "type": a.get("desc") or a.get("t", "")},
                "links": links}

    def activity(self):
        parts = [super().activity()]
        n, msgs = getattr(self, "_count", (0, 0))
        parts.append(f"{n} aircraft, {msgs} messages")
        try:
            st = json.loads((self._dir / "stats.json").read_text())
            loc = st.get("last1min", {}).get("local", {})
            if "signal" in loc and "noise" in loc:
                parts.append(f"signal {loc['signal']:.0f} dB, noise {loc['noise']:.0f} dB")
            ppm = st.get("estimated_ppm")
            # readsb measures it every 30 s (samples received against the PC's clock);
            # a new value means a new measurement.
            if self.live() and ppm is not None and ppm != self._ppm_last and (ppm != 0 or self._ppm_last is not None):
                self._ppm_last = ppm
                self._ppm_readings = (self._ppm_readings + [float(ppm)])[-10:]
            if self._ppm_readings:
                parts.append(f"dongle clock {self._ppm_readings[-1]:+.1f} ppm off after correction")
        except (OSError, ValueError, AttributeError):
            pass
        return " · ".join(parts)

    def ppm_suggestion(self):
        """(new correction, readings used) once two or more readings agree, else None.

        readsb runs with the current correction applied (librtlsdr corrects the sample clock
        with it too), so its readings are what's left over: the new value adds them on.
        """
        r = self._ppm_readings[-4:] if hasattr(self, "_ppm_readings") else []
        if len(r) < 2 or max(r) - min(r) > 3:
            return None
        return int(round(int(self.values["ppm"]) + sum(r) / len(r))), len(r)

    def upstream(self):
        return self.relay_stream(1090e6, 2400000) if self.bridge() else None

    def actions(self):
        if not self.live() or self.bridge():
            return []                      # the relay's dongle: its owner sets its correction
        s = self.ppm_suggestion()
        if s is None:
            n = len(getattr(self, "_ppm_readings", []))
            return [("ppm", f"Measuring the dongle's clock… ({n} of 2 readings, one every 30 s)", False,
                     "readsb compares the samples the dongle delivers with your PC's clock.")]
        value, n = s
        if value == int(self.values["ppm"]):
            return [("ppm", f"Frequency correction {value:+d} ppm is right (measured)", False,
                     f"The last {n} measurements agree with the current setting.")]
        return [("ppm", f"Set frequency correction to {value:+d} ppm", True,
                 f"From {n} measurements of the dongle's clock against your PC's. Applies to every "
                 "RTL-SDR mode.")]

    def run_action(self, action_id):
        s = self.ppm_suggestion()
        if action_id == "ppm" and s:
            return {"ppm": s[0]}
        return {}

    def stop(self):
        events = super().stop()
        events += self._read_json()
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)
        return events

    def finished(self, code):
        if not self.live() and not self.bridge():
            return self._read_json() + [Event("status", "recording finished")]
        return super().finished(code)
