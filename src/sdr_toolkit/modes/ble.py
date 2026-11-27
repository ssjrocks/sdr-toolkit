"""Bluetooth LE devices: every phone, watch, earbud, tracker, beacon, sensor and drone this PC's Bluetooth hears,
what each one says about itself, how strongly and for how long. Trackers (AirTags and other Find My devices away
from their owner, Samsung SmartTags, Tiles) are marked, and drones' Remote ID (their ID, position, height, speed
and their pilot's position) goes on the map. No SDR: the PC's own Bluetooth (engines/ble_scan.py)."""

import json
import time

from .. import system
from ..engines.ble import classify, remote_id
from .base import Event, Setting, Tuning
from .process import ProcessMode
from .radio import LOCATION_SETTINGS, home

FOLLOW_MIN = 20 * 60          # s: a tracker heard this long is pointed out


class BLEMode(ProcessMode):
    id = "ble"
    name = "Bluetooth LE devices and drone Remote ID"
    group = "Wi-Fi & Bluetooth"
    source = "none"                 # no audio, no SDR: the PC's Bluetooth
    summary = ("Every Bluetooth LE device around: phones, watches, earbuds, trackers (AirTags, SmartTags, Tiles), "
               "beacons and sensors, with what each says, its signal and how long it's been around. Drones' "
               "Remote ID on the map. Uses this PC's Bluetooth.")
    output = "table"
    map = True
    key_column = "address"
    columns = (("address", "Address"), ("name", "Name"), ("what", "What"), ("maker", "Maker"), ("rssi", "dBm"),
               ("around", "Around for"), ("count", "Heard"), ("last", "Last heard"))
    settings = LOCATION_SETTINGS + (
        Setting("forget", "Drop devices after", 10, "int", 1, 240, 1, suffix=" min",
                help="Devices not heard for this long leave the list (phones change their address every "
                     "15 minutes or so, so one phone can show as several)."),
        Setting("only", "Show", "all", "choice", choices=(("all", "Everything"), ("trackers", "Trackers only"),
                                                           ("drones", "Drones only"), ("named", "Devices with a name"))),
    )
    tuning = Tuning(
        sdr_mode="No SDR: this PC's Bluetooth adapter scans (BlueZ on Linux, Windows' own on Windows)",
        bandwidth="2.4 GHz, Bluetooth LE advertising channels 37-39",
        frequencies=(("2402 / 2426 / 2480 MHz", "Bluetooth LE advertising"),),
        notes="Most phones change their Bluetooth address every 15 minutes or so, and many devices only advertise "
              "when they're looking for something. Trackers are marked; one that stays around for "
              f"{FOLLOW_MIN // 60} minutes or more is pointed out. Remote ID is required on most drones in many "
              "countries (US, EU, Japan, Australia for some).",
    )

    def live(self) -> bool:
        return True

    def command(self):
        self._devices: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._pointed = set()
        return system.python_command("sdr_toolkit.engines.ble_scan")

    def parse(self, line):
        if not line.startswith("{"):
            if "ble_scan:" in line:
                return [Event("line", line.strip(), style="warn"), Event("status", line.strip())]
            return []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        if e.get("event") == "scanning":
            return [Event("status", "Scanning for Bluetooth LE devices")]
        if e.get("event") != "adv":
            return []
        return self._adv(e, time.time())

    def _adv(self, e, now):
        man = {int(k): bytes.fromhex(v) for k, v in e.get("manufacturer", {}).items()}
        sd = {k: bytes.fromhex(v) for k, v in e.get("service_data", {}).items()}
        c = classify(e.get("name", ""), man, e.get("services", []), sd)
        addr = e["address"]
        d = self._devices.get(addr)
        events = []
        if d is None:
            d = self._devices[addr] = {"first": now, "count": 0, "rssi_max": -999}
            label = c["what"] or e.get("name") or c["maker"] or "device"
            if c["tracker"]:
                events.append(Event("line", f"Tracker: {label} ({addr}, {e.get('rssi')} dBm)", style="warn"))
        d.update(name=e.get("name") or d.get("name", ""), rssi=e.get("rssi"), last=now, what=c["what"] or d.get("what", ""),
                 maker=c["maker"] or d.get("maker", ""), kind=c["kind"] or d.get("kind", ""),
                 tracker=c["tracker"] or d.get("tracker", False), extra=c["extra"] or d.get("extra", {}),
                 services=e.get("services", []), manufacturer=e.get("manufacturer", {}), tx_power=e.get("tx_power"))
        d["count"] += 1
        if isinstance(e.get("rssi"), (int, float)):
            d["rssi_max"] = max(d["rssi_max"], e["rssi"])
        rid = remote_id(sd)
        if rid:
            new = "rid" not in d
            d.setdefault("rid", {}).update(rid)
            d["what"], d["kind"] = "Drone (Remote ID)", "drone"
            if new:
                r = d["rid"]
                events.append(Event("line", "Drone Remote ID: " + " · ".join(
                    x for x in (r.get("id"), r.get("ua_type"), r.get("operator")) if x), style="warn"))
            r = d["rid"]
            if "lat" in r:
                events.append(Event("log", f"Drone {r.get('id') or addr}: {r['lat']:.5f}, {r['lon']:.5f}"
                                           + (f", {r['height']:.0f} m up" if r.get("height") is not None else "")
                                           + (f", {r['speed_ms']:.1f} m/s" if r.get("speed_ms") is not None else ""),
                                    style="pos"))
        if d.get("tracker") and now - d["first"] >= FOLLOW_MIN and addr not in self._pointed:
            self._pointed.add(addr)
            events.append(Event("line", f"The tracker {d['what'] or addr} has been around for "
                                        f"{(now - d['first']) / 60:.0f} minutes", style="warn"))
        self._dirty = True
        return events

    def poll(self):
        events = super().poll()
        now = time.time()
        if getattr(self, "_dirty", False) and now - self._last_table >= 1.0:
            self._dirty = False
            self._last_table = now
            events += self._views(now)
        return events

    def _views(self, now):
        ttl = int(self.values.get("forget", 10)) * 60
        self._devices = {k: d for k, d in self._devices.items() if now - d["last"] < ttl}
        only = self.values.get("only", "all")
        rows, points = [], []
        for addr, d in sorted(self._devices.items(), key=lambda kv: -(kv[1].get("rssi") or -999)):
            if (only == "trackers" and not d.get("tracker")) or (only == "drones" and "rid" not in d) or \
                    (only == "named" and not d.get("name")):
                continue
            mins = (now - d["first"]) / 60
            rows.append({"address": addr, "name": d.get("name", ""),
                         "what": ("⚠ " if d.get("tracker") else "") + (d.get("what") or ""), "maker": d.get("maker", ""),
                         "rssi": d.get("rssi", ""), "around": f"{mins:.0f} min" if mins >= 1 else "<1 min",
                         "count": d["count"], "last": time.strftime("%H:%M:%S", time.localtime(d["last"]))})
            r = d.get("rid") or {}
            if "lat" in r:
                points.append({"id": addr, "lat": r["lat"], "lon": r["lon"], "kind": "aircraft",
                               "label": r.get("id") or "drone", "heading": r.get("track")})
            if "op_lat" in r:
                points.append({"id": addr + "-pilot", "lat": r["op_lat"], "lon": r["op_lon"], "kind": "landing",
                               "label": "pilot of " + (r.get("id") or "drone")})
        return [Event("table", data={"rows": rows}), Event("points", data={"points": points, "home": home(self.values)})]

    def stop(self):
        events = super().stop()
        if getattr(self, "_devices", None):
            events += self._views(time.time())
        return events

    def details(self, key):
        d = getattr(self, "_devices", {}).get(key)
        if d is None:
            return None
        f = [("Address", key), ("Name", d.get("name", "")), ("What", d.get("what", "")), ("Maker", d.get("maker", "")),
             ("Signal", f"{d.get('rssi')} dBm now, {d['rssi_max']} dBm at its strongest"),
             ("Around for", f"{(time.time() - d['first']) / 60:.0f} min"), ("Advertisements", str(d["count"]))]
        if d.get("tracker"):
            f.append(("Tracker", "yes: it can be located by its owner's network"))
        if d.get("tx_power") is not None:
            f.append(("Transmit power", f"{d['tx_power']} dBm"))
        for k, v in (d.get("extra") or {}).items():
            f.append((k, str(v)))
        if d.get("services"):
            f.append(("Services", ", ".join(d["services"][:8])))
        for cid, hexdata in (d.get("manufacturer") or {}).items():
            f.append((f"Maker data 0x{int(cid):04X}", hexdata[:64]))
        for k, label in (("id", "Drone ID"), ("id_type", "ID type"), ("ua_type", "Aircraft type"), ("status", "Status"),
                         ("height", "Height (m)"), ("alt_geo", "Altitude (m)"), ("speed_ms", "Speed (m/s)"),
                         ("climb_ms", "Climb (m/s)"), ("track", "Track (°)"), ("operator", "Operator ID"),
                         ("description", "Description")):
            v = (d.get("rid") or {}).get(k)
            if v not in (None, ""):
                f.append((label, str(v)))
        r = d.get("rid") or {}
        if "lat" in r:
            f.append(("Drone position", f"{r['lat']:.6f}, {r['lon']:.6f}"))
        if "op_lat" in r:
            f.append(("Pilot position", f"{r['op_lat']:.6f}, {r['op_lon']:.6f}"))
        return {"title": d.get("name") or d.get("what") or key, "subtitle": d.get("maker", ""),
                "fields": [x for x in f if x[1] not in ("", None)]}

    def listening_on(self):
        return "this PC's Bluetooth"
