"""406 MHz distress beacons: EPIRBs (ships), PLBs (people) and ELTs (aircraft) as COSPAS-SARSAT satellites hear
them. Each beacon's identity (country, MMSI, aircraft address or serial number, 15-hex ID) and, from location
protocols, the GPS position it sends. Self-tests (one burst when someone presses the test button) are marked.

First-generation beacons (nearly all in use) are decoded by the toolkit (engines/beacon406.py)."""

import json
import time

from .. import system
from .base import Event, Setting, Tuning
from .radio import LOCATION_SETTINGS, RADIO_SETTINGS, RadioMode, home

CENTRE = 405.85e6           # 406.0-406.1 MHz sits 150-250 kHz above, clear of the SDR's DC spike
RATE = 960000


class Beacon406Mode(RadioMode):
    id = "beacons406"
    name = "406 MHz distress beacons (EPIRB, PLB, ELT)"
    group = "SDR"
    summary = ("COSPAS-SARSAT distress beacons on 406 MHz: who a beacon belongs to (country, MMSI, aircraft "
               "address or serial) and the GPS position it sends, on the map. Self-tests are marked as such.")
    output = "table"
    requires = ("rtl-sdr",)
    map = True
    key_column = "hex_id"
    columns = (("hex_id", "Beacon (15-hex ID)"), ("kind", "Type"), ("ident", "Identity"), ("country", "Country"),
               ("pos", "Position"), ("test", "Self-test"), ("count", "Bursts"), ("last", "Last heard"))
    iq_rate = RATE
    settings = RADIO_SETTINGS + LOCATION_SETTINGS + (
        Setting("file_rate", "Recording sample rate", RATE, "int", 120000, 9600000, 120000, suffix=" Hz",
                restart=True, help="A multiple of 120 kHz."),
        Setting("file_centre_mhz", "Recording centre frequency", CENTRE / 1e6, "float", 400.0, 410.0, 0.001, 4,
                suffix=" MHz", restart=True, help="406.0-406.1 MHz must be inside the recording."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the SDR around 405.85 MHz; the 406.0-406.1 MHz band is watched for bursts",
        bandwidth="Each beacon is about 3 kHz wide; the band is 100 kHz",
        frequencies=(("406.0-406.1 MHz", "COSPAS-SARSAT distress beacons (channels every 3 kHz)"),),
        notes="A beacon sends a half-second burst every 50 s once it's set off, for a day or more. Most bursts "
              "you'll hear are self-tests: one burst when an owner presses the test button, marked here. A "
              "beacon that isn't a self-test may be a real distress (or an accidental activation): the "
              "satellites relay it to rescue authorities already. Heard from tens of km with line of sight.",
    )

    def _rate(self):
        return RATE if self.live() else int(self.values["file_rate"])

    def listening_on(self):
        return "406.0-406.1 MHz"

    def upstream(self):
        if not self.live():
            return None
        if self.bridge():
            return self.relay_stream(CENTRE, RATE)
        cmd = ["rtl_sdr", "-d", self.device_index().lstrip(":"), "-f", str(int(CENTRE)), "-s", str(RATE),
               "-p", str(self.values["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def command(self):
        centre = CENTRE if self.live() else float(self.values["file_centre_mhz"]) * 1e6
        self._beacons: dict[str, dict] = {}
        self._bursts = 0
        self._dirty = False
        self._last_table = 0.0
        return system.python_command("sdr_toolkit.engines.beacon406") + [
            "--input", "-" if self.live() else self.file_path(), "--rate", str(self._rate()),
            "--centre", str(int(centre))]

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            return [Event("line", line.strip(), style="warn")] if "beacon406:" in line else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        if e.get("event") != "beacon":
            return []
        if not e.get("bch_ok"):
            return [Event("log", f"A burst on {e['freq'] / 1e6:.4f} MHz failed its error check", style="dim")]
        self._bursts += 1
        key = e["hex_id"]
        now = time.time()
        new = key not in self._beacons
        b = self._beacons.setdefault(key, {"count": 0, "first": now})
        b.update(e)
        b["count"] += 1
        b["last"] = now
        b["seq"] = self._bursts
        self._dirty = True
        where = f" at {e['lat']:.4f}, {e['lon']:.4f}" if "lat" in e else ""
        text = (f"{'Self-test' if e['self_test'] else 'BEACON'} {key} ({e.get('protocol', '')}, "
                f"{e.get('country', '')}): {e.get('id', '')}{where} on {e['freq'] / 1e6:.4f} MHz")
        events = [Event("log", text)]
        if new:
            events.append(Event("line", text, style="" if e["self_test"] else "warn"))
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
        if getattr(self, "_beacons", None):
            events += self._views()
        return events

    def _views(self):
        rows, points = [], []
        for key, b in sorted(self._beacons.items(), key=lambda kv: (-kv[1]["last"], -kv[1]["seq"])):
            pos = f"{b['lat']:.4f}, {b['lon']:.4f}" if "lat" in b else ""
            rows.append({"hex_id": key, "kind": b.get("protocol", ""), "ident": b.get("id", ""),
                         "country": b.get("country", ""), "pos": pos, "test": "yes" if b["self_test"] else "",
                         "count": b["count"], "last": time.strftime("%H:%M:%S", time.localtime(b["last"]))})
            if "lat" in b:
                points.append({"id": key, "lat": b["lat"], "lon": b["lon"], "kind": "ship",
                               "label": f"{'Test ' if b['self_test'] else ''}{b.get('id') or key}"})
        return [Event("table", data={"rows": rows}),
                Event("points", data={"points": points, "home": home(self.values)})]

    def details(self, key):
        b = getattr(self, "_beacons", {}).get(key)
        if b is None:
            return None
        f = [("15-hex ID", key), ("Protocol", b.get("protocol", "")), ("Identity", b.get("id", "")),
             ("Country", f"{b.get('country', '')} ({b.get('country_code', '')})"),
             ("Message", "self-test" if b["self_test"] else "distress (or an accidental activation)"),
             ("Frame", "long (144 bits)" if b.get("long") else "short (112 bits)"),
             ("Frequency", f"{b['freq'] / 1e6:.4f} MHz"), ("Bursts heard", str(b["count"]))]
        for k, label in (("gps", "Position from"), ("activation", "Activated"), ("icao", "Aircraft address")):
            if b.get(k):
                f.append((label, str(b[k])))
        if "homing_121" in b:
            f.append(("121.5 MHz homing", "yes" if b["homing_121"] else "no"))
        if "lat" in b:
            f.append(("Position", f"{b['lat']:.5f}, {b['lon']:.5f}"))
        f.append(("Frame bits", b.get("bits", "")))
        return {"title": b.get("id") or key, "subtitle": b.get("protocol", ""), "fields": f}

    def activity(self):
        return super().activity() + f" · {len(getattr(self, '_beacons', {}))} beacons, {getattr(self, '_bursts', 0)} bursts"
