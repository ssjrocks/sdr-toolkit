"""Every FM broadcast station in the band, with its RDS: the station's name, radiotext (song, show, message),
programme type, traffic flags, alternative frequencies and clock. The engine (engines/fm_band.py) steps the SDR
across 87.5-108 MHz (2 MHz at a time on an RTL-SDR, 16 MHz on a HackRF) and reads every station in view."""

import json
import time

from .. import system
from .base import Event, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode


class FMStationsMode(RadioMode):
    id = "fm_stations"
    name = "FM stations and RDS"
    group = "SDR"
    summary = ("Every FM broadcast station in the band with its RDS: name, radiotext (what's playing), programme "
               "type, traffic announcements, alternative frequencies and clock.")
    output = "table"
    requires = ("rtl-sdr",)
    key_column = "freq"
    columns = (("freq", "MHz"), ("ps", "Station"), ("rt", "Radiotext"), ("pty", "Programme"), ("pi", "PI"),
               ("flags", "Flags"), ("snr", "SNR dB"), ("last", "Last heard"))
    settings = RADIO_SETTINGS + (
        Setting("dwell", "Time on each part of the band", 20, "int", 5, 300, 5, suffix=" s", restart=True,
                help="How long the SDR stays on each window of the band. A station's name comes within a "
                     "second or two; a whole radiotext can take 10-20 s."),
        Setting("lo_mhz", "From", 87.5, "float", 64.0, 108.0, 0.1, 1, suffix=" MHz", restart=True),
        Setting("hi_mhz", "To", 108.0, "float", 64.0, 108.0, 0.1, 1, suffix=" MHz", restart=True),
        Setting("rbds", "North American programme types (RBDS)", False, "bool",
                help="RDS's programme-type list (Europe, Australia, most places), or RBDS's (US, Canada)."),
        Setting("file_rate", "Recording sample rate", 2400000, "int", 240000, 20000000, 240000, suffix=" Hz",
                restart=True, help="A multiple of 240 kHz."),
        Setting("file_centre_mhz", "Recording centre frequency", 98.0, "float", 64.0, 110.0, 0.001, 3,
                suffix=" MHz", restart=True),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ: the SDR steps across the FM band and every station in view is decoded at once",
        bandwidth="Each station is about 200 kHz wide; the SDR sees 2 MHz (RTL-SDR) or 16 MHz (HackRF) at a time",
        frequencies=(("87.5-108 MHz", "FM broadcast"),),
        notes="RDS is weaker than the station's audio: a station you can hear clearly may still need a better "
              "antenna (outdoors, or a dipole about 75 cm each side) for its radiotext. To listen to a station, "
              "tune it in WFM in Spectrum & receiver, which shows its RDS too.",
    )

    def _rate(self) -> int:
        if not self.live():
            return int(self.values["file_rate"])
        b = self.bridge()
        return 19_200_000 if b is not None and b.device.get("type") == "hackrf" else 2_400_000

    def listening_on(self):
        return f"{float(self.values['lo_mhz']):g}-{float(self.values['hi_mhz']):g} MHz"

    def command(self):
        v = self.values
        self._stations: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._window = ""
        if not self.live():
            return system.python_command("sdr_toolkit.engines.fm_band") + [
                "--input", self.file_path(), "--rate", str(self._rate()),
                "--centre", str(int(float(v["file_centre_mhz"]) * 1e6))]
        b = self.bridge()
        self._port = b.local_port if b else self._free_port()
        cmd = system.python_command("sdr_toolkit.engines.fm_band") + [
            "--port", str(self._port), "--rate", str(self._rate()), "--dwell", str(v["dwell"]),
            "--lo", str(v["lo_mhz"]), "--hi", str(v["hi_mhz"])]
        if self.gain_db():
            cmd += ["--gain", self.gain_db()]
        if b:
            return cmd
        self._start_rtl_tcp()
        return self._wait_for_rtl_tcp(cmd)

    def stop(self):
        events = super().stop()
        self._stop_rtl_tcp()
        if getattr(self, "_stations", None):
            events.append(self._table())
        return events

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            return [Event("line", line.strip(), style="warn")] if "fm_band:" in line else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        if e.get("event") == "window":
            self._window = f"{e['lo']:g}-{e['hi']:g} MHz"
            return [Event("status", self.activity())]
        if e.get("event") != "station":
            return []
        key = f"{float(e['freq']):.1f}"
        s = self._stations.setdefault(key, {"first": time.time()})
        events = []
        had_ps, had_rt = s.get("ps", ""), s.get("rt", "")
        s.update(e)
        s["last"] = time.time()
        if e.get("ps") and e["ps"] != had_ps:
            events.append(Event("log", f"{key} MHz: {e['ps']}" + (f" ({e['pty']})" if e.get("pty") else "")))
        if e.get("rt") and e["rt"] != had_rt:
            events.append(Event("log", f"{key} MHz {e.get('ps') or ''}: {e['rt']}".replace("  ", " ")))
        self._dirty = True
        return events

    def poll(self):
        events = super().poll()
        if getattr(self, "_dirty", False) and time.time() - self._last_table >= 1.0:
            self._dirty = False
            self._last_table = time.time()
            events.append(self._table())
        return events

    def _table(self):
        from ..engines.rds import PTY_RBDS, PTY_RDS
        rows = []
        for key, s in sorted(getattr(self, "_stations", {}).items(), key=lambda kv: float(kv[0])):
            has_rds = bool(s.get("pi"))
            flags = [x for x, on in (("stereo", s.get("pilot")), ("TP", has_rds and s.get("tp")),
                                     ("TA", has_rds and s.get("ta"))) if on]
            pty = s.get("pty", "") if has_rds else ""
            if pty and self.values.get("rbds") and pty in PTY_RDS:
                pty = PTY_RBDS[PTY_RDS.index(pty)]
            rows.append({"freq": key, "ps": s.get("ps", ""), "rt": s.get("rt", ""), "pty": pty,
                         "pi": s.get("pi", ""), "flags": " ".join(flags), "snr": s.get("snr", ""),
                         "last": time.strftime("%H:%M:%S", time.localtime(s["last"]))})
        return Event("table", data={"rows": rows})

    def details(self, key):
        s = getattr(self, "_stations", {}).get(key)
        if s is None:
            return None
        f = [("Frequency", f"{key} MHz")]
        for k, label in (("ps", "Name (PS)"), ("rt", "Radiotext"), ("pty", "Programme type"),
                         ("ptyn", "Programme type name"), ("pi", "PI code"), ("clock", "Clock (CT)")):
            if s.get(k):
                f.append((label, str(s[k])))
        if s.get("pi"):
            f += [("Traffic programme (TP)", "yes" if s.get("tp") else "no"),
                  ("Traffic announcement now (TA)", "yes" if s.get("ta") else "no"),
                  ("Music or speech", "music" if s.get("music") else "speech")]
        if s.get("af"):
            f.append(("Alternative frequencies", ", ".join(f"{x:.1f}" for x in s["af"])))
        f += [("Stereo pilot", "yes" if s.get("pilot") else "no"), ("Signal", f"{s.get('snr', '?')} dB above the noise"),
              ("RDS groups heard", str(s.get("groups", 0)))]
        if s.get("group_types"):
            f.append(("Group types", ", ".join(f"{k} ×{v}" for k, v in sorted(s["group_types"].items()))))
        return {"title": s.get("ps") or f"{key} MHz", "subtitle": f"{key} MHz" + (f" · PI {s['pi']}" if s.get("pi") else ""),
                "fields": f}

    def activity(self):
        n = len(getattr(self, "_stations", {}))
        named = sum(1 for s in getattr(self, "_stations", {}).values() if s.get("ps"))
        where = f" · now {self._window}" if getattr(self, "_window", "") else ""
        return super().activity() + f" · {n} stations, {named} with RDS{where}"
