"""Satellite pictures and data through SatDump: METEOR-M weather pictures (LRPT, 137 MHz), recorded pass by pass
by itself, and Inmarsat STD-C (maritime safety and messages) and AERO (aircraft messages) from a geostationary
satellite. SatDump runs without its window (engines/satdump_run.py); the pictures go to the logbook."""

import json
import time
from pathlib import Path

from .. import system
from .base import Event, MissingParts, Setting, Tuning
from .radio import LOCATION_SETTINGS, RADIO_SETTINGS, RadioMode, home

TARGETS = {
    # id: (label, pipeline, sample rate, NORAD numbers, default frequency MHz)
    "meteor": ("METEOR-M weather pictures (LRPT 72k, 137 MHz)", "meteor_m2-x_lrpt", 1.024e6, "57166,59051", 137.9),
    "meteor80": ("METEOR-M weather pictures (LRPT 80k, 137 MHz)", "meteor_m2-x_lrpt_80k", 1.024e6, "57166,59051",
                 137.9),
    "stdc": ("Inmarsat STD-C (1.54 GHz)", "inmarsat_std_c", 1.024e6, "", 1541.45),
    "aero6": ("Inmarsat AERO 600 bit/s (1.545 GHz)", "inmarsat_aero_6", 1.024e6, "", 1545.0),
    "aero12": ("Inmarsat AERO 1200 bit/s (1.545 GHz)", "inmarsat_aero_12", 1.024e6, "", 1545.0),
    "aero105": ("Inmarsat AERO 10.5k (1.545 GHz)", "inmarsat_aero_105", 1.024e6, "", 1545.0),
}


class SatDumpMode(RadioMode):
    id = "satdump"
    name = "Weather satellites and Inmarsat (SatDump)"
    group = "Satellites"
    summary = ("METEOR-M weather pictures, recorded by themselves on every pass over you, and Inmarsat STD-C and "
               "AERO messages from a geostationary satellite. Decoded by SatDump.")
    output = "table"
    requires = ("satdump",)
    key_column = "key"
    columns = (("when", "When"), ("what", "Satellite"), ("result", "Result"), ("detail", "Details"), ("key", "ID"))
    settings = RADIO_SETTINGS + LOCATION_SETTINGS + (
        Setting("target", "Receive", "meteor", "choice", restart=True,
                choices=tuple((k, v[0]) for k, v in TARGETS.items())),
        Setting("freq_mhz", "Frequency", 137.9, "float", 100.0, 2000.0, 0.001, 4, suffix=" MHz", restart=True,
                help="METEOR-M2-3 and M2-4 send LRPT on 137.9 or 137.1 MHz, at 72k or 80k (each uses one at a time, "
                     "and it changes now and then: SatNOGS lists the current ones). "
                     "Inmarsat: the STD-C or AERO channel of the satellite you see (e.g. from a spectrum view)."),
        Setting("min_el", "Lowest pass", 20, "int", 0, 80, 5, suffix="°", restart=True,
                help="METEOR passes that don't climb this high are skipped (low passes give little picture)."),
        Setting("file_rate", "Recording sample rate", 1024000, "int", 48000, 20000000, 1000, suffix=" Hz",
                restart=True),
    )
    tuning = Tuning(
        sdr_mode="SatDump runs the SDR itself (through rtl_tcp for a HackRF or a relay)",
        bandwidth="METEOR LRPT 120 kHz (1.024 MS/s); Inmarsat channels a few kHz",
        frequencies=(("137.1 / 137.9 MHz", "METEOR-M2-3 and M2-4 LRPT pictures"),
                     ("1537-1545 MHz", "Inmarsat STD-C and AERO (from a geostationary satellite)")),
        notes="METEOR: a V-dipole or QFH for 137 MHz with a clear view of the sky; a pass lasts 10-15 minutes and "
              "the picture comes when it ends. Your location is needed for the pass times (satellite tracker's "
              "elements). Inmarsat needs an L-band patch antenna pointed at the satellite and an LNA.",
    )

    def _target(self):
        return TARGETS[self.values["target"]]

    def listening_on(self):
        return f"{float(self.values['freq_mhz']):g} MHz"

    def command(self):
        import shutil
        if shutil.which("satdump") is None:
            raise MissingParts(["satdump"])
        v = self.values
        label, pipeline, rate, norads, _ = self._target()
        self._rows: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._next = None
        self._progress = None
        self._recording = None
        out = system.data_dir() / "satdump"
        if self.live():
            b = self.bridge()
            src = f"rtltcp:127.0.0.1:{b.local_port}" if b else "rtlsdr"
        else:
            src = self.file_path()
            rate = float(v["file_rate"])
        cmd = system.python_command("sdr_toolkit.engines.satdump_run") + [
            "--pipeline", pipeline, "--frequency", str(float(v["freq_mhz"]) * 1e6), "--samplerate", str(rate),
            "--source", src, "--out", str(out)]
        if self.live() and self.gain_db():
            cmd += ["--gain", self.gain_db()]
        if norads and self.live():
            here = home(v)
            if here is None:
                raise RuntimeError("METEOR passes need your location: Settings → Your latitude / longitude "
                                   "(the GPS position fix page can fill it in).")
            cmd += ["--norad", norads, "--lat", str(here[0]), "--lon", str(here[1]), "--min-el", str(v["min_el"])]
        return cmd

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            return [Event("line", line.strip(), style="warn")] if "satdump_run:" in line else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        kind = e.get("event")
        if kind == "next":
            self._next = e
            when = time.strftime("%H:%M", time.localtime(e["aos"]))
            return [Event("line", f"Next pass: {e['name']} at {when}, up to {e['max_el']:.0f}°"),
                    Event("status", self.activity())]
        if kind == "start":
            self._recording = e
            self._progress = None
            return [Event("line", f"Recording {e['name']}" + (" (the picture comes when the pass ends)"
                                                              if self.values["target"].startswith("meteor") else ""))]
        if kind == "progress":
            self._progress = e
            return []
        if kind == "log":
            return [Event("log", f"SatDump: {e['text']}", style="dim")]
        if kind == "pass":
            self._recording = None
            key = Path(e["dir"]).name
            imgs = e.get("images", [])
            result = (f"{len(imgs)} pictures" if imgs else "no picture") + (", locked" if e.get("locked") else "")
            detail = (f"peak SNR {e['peak_snr']:.1f} dB" if isinstance(e.get("peak_snr"), (int, float)) else "") + \
                     (f", up to {e['max_el']:.0f}°" if e.get("max_el") else "")
            self._rows[key] = {"key": key, "when": time.strftime("%d %b %H:%M"), "what": e["name"], "result": result,
                               "detail": detail, "images": imgs, "dir": e["dir"]}
            self._dirty = True
            events = [Event("line", f"{e['name']}: {result}" + (f" ({detail})" if detail else ""))]
            for img in imgs:
                events.append(Event("file", data={"path": img, "kind": "picture",
                                                  "title": f"{e['name']} {Path(img).stem}",
                                                  "info": {"source": "SatDump", "pass": key}}))
            return events
        if kind == "message":
            key = Path(e["path"]).name
            text = " ".join(e.get("text", "").split())
            self._rows[key] = {"key": key, "when": time.strftime("%d %b %H:%M"), "what": self._target()[0].split(" (")[0],
                               "result": "message", "detail": text[:160], "path": e["path"]}
            self._dirty = True
            return [Event("log", f"{self._target()[0].split(' (')[0]}: {text[:300]}")]
        return []

    def poll(self):
        events = super().poll()
        if getattr(self, "_dirty", False) and time.time() - self._last_table >= 1.0:
            self._dirty = False
            self._last_table = time.time()
            rows = sorted(self._rows.values(), key=lambda r: r["key"], reverse=True)
            events.append(Event("table", data={"rows": [{k: r[k] for k in ("key", "when", "what", "result", "detail")}
                                                        for r in rows]}))
        return events

    def details(self, key):
        r = getattr(self, "_rows", {}).get(key)
        if r is None:
            return None
        f = [("When", r["when"]), ("Satellite", r["what"]), ("Result", r["result"])]
        if r.get("detail"):
            f.append(("Details", r["detail"]))
        if r.get("dir"):
            f.append(("Folder", r["dir"]))
        if r.get("path"):
            f.append(("File", r["path"]))
        out = {"title": r["what"], "subtitle": r["when"], "fields": f}
        imgs = r.get("images") or []
        if imgs:
            # SatDump's main picture: a colour composite if there is one, else the first channel.
            best = next((i for i in imgs if "rgb" in Path(i).name.lower() or "221" in Path(i).name), imgs[0])
            out["image"], out["image_caption"] = best, Path(best).name
        return out

    def activity(self):
        s = super().activity()
        rec, nxt, pr = getattr(self, "_recording", None), getattr(self, "_next", None), getattr(self, "_progress", None)
        if rec:
            s += f" · recording {rec['name']}"
            if pr:
                s += f" · SNR {pr.get('snr') or 0:.1f} dB, {pr.get('lock', '')}"
        elif nxt:
            mins = (nxt["aos"] - time.time()) / 60
            s += f" · next: {nxt['name']} in {mins:.0f} min (up to {nxt['max_el']:.0f}°)"
        return s
