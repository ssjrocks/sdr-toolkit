"""VOR and ILS: the radio beacons aircraft navigate by. A VOR tells you which radial (bearing from the station)
you're on; an ILS localizer how far left or right of a runway's centre line, its glide slope how far above or
below the approach path. Each beacon also sends its ident in Morse.

The engine (engines/navaid.py) watches every navaid channel in what the SDR sees (2 MHz of the 108-118 MHz
band on an RTL-SDR, up to 8 on a HackRF) and measures the ones it hears."""

import json
import time

from .. import system
from ..engines.navaid import GS_PAIRS, is_localizer
from .base import Event, Setting, Tuning
from .radio import RADIO_SETTINGS, RTL_MAX_RATE, RadioMode

LOC_FULL_SCALE = 0.155        # DDM at a full-scale needle (ICAO): about 2.5° off a localizer's course
GS_FULL_SCALE = 0.175         # and about 0.7° off a glide slope's path


def reading(e: dict) -> str:
    kind = e.get("kind")
    if kind == "VOR":
        r = float(e["radial"])
        return f"Radial {r:03.0f}° · to the station {(r + 180) % 360:03.0f}°"
    if kind in ("LOC", "GS"):
        ddm = float(e["ddm"])
        full = LOC_FULL_SCALE if kind == "LOC" else GS_FULL_SCALE
        pct = min(abs(ddm) / full * 100, 999)
        if abs(ddm) < 0.01:
            where = "on course" if kind == "LOC" else "on the glide path"
        elif kind == "LOC":
            where = "left of course, fly right" if ddm > 0 else "right of course, fly left"
        else:
            where = "above the path, fly down" if ddm > 0 else "below the path, fly up"
        return f"DDM {ddm:+.3f}: {where} ({pct:.0f}% of full scale)"
    return "A carrier without VOR or ILS tones"


class NavaidMode(RadioMode):
    id = "navaids"
    name = "VOR and ILS beacons"
    group = "SDR"
    summary = ("Aircraft navigation beacons: a VOR's radial (your bearing from the station), an ILS localizer's "
               "and glide slope's left/right and up/down deviation, and each beacon's Morse ident. Every channel "
               "in view at once.")
    output = "table"
    requires = ("rtl-sdr",)
    key_column = "freq"
    columns = (("freq", "MHz"), ("kind", "Type"), ("ident", "Ident"), ("reading", "Reading"), ("snr", "SNR dB"),
               ("last", "Last heard"))
    settings = RADIO_SETTINGS + (
        Setting("centre_mhz", "Centre frequency", 113.0, "float", 100.0, 340.0, 0.1, 3, suffix=" MHz", restart=True,
                help="The SDR watches the band around this. VORs are on 108-117.95 MHz (the even tenths below "
                     "112), localizers on the odd tenths of 108.1-111.95, glide slopes on 328.6-335.4 MHz. The "
                     "recording's centre for a recording."),
        Setting("freqs", "Frequencies", "", "text", restart=True,
                help="Empty: every 50 kHz navaid channel in view. Or MHz to measure, separated by commas."),
        Setting("rate", "Sample rate", 2400000, "choice", restart=True, per_sdr=True,
                choices=((2400000, "2.4 MS/s (2 MHz of band)"), (4800000, "4.8 MS/s (4 MHz, HackRF)"),
                         (9600000, "9.6 MS/s (8 MHz, HackRF)"))),
        Setting("file_rate", "Recording sample rate", 2400000, "int", 48000, 20000000, 48000, suffix=" Hz",
                restart=True, help="A multiple of 48 kHz."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the SDR; every VOR / ILS channel in it is measured by itself",
        bandwidth="Each beacon is about 25 kHz wide; the SDR watches 2 MHz (RTL-SDR) to 8 MHz (HackRF)",
        frequencies=(("108-117.95 MHz", "VORs (and localizers on 108.1-111.95, odd tenths)"),
                     ("328.6-335.4 MHz", "ILS glide slopes, paired with their localizers"),
                     ("118-136 MHz", "ATIS (an airport's recorded weather and runway): listen on AM in the "
                                     "Receiver page")),
        notes="VORs reach 100-300 km at height but much less on the ground: a station within line of sight, "
              "or an outdoor antenna, is needed. Localizers and glide slopes point along a runway and are "
              "heard near the airport. Radials are magnetic. The ident is sent every 10-30 s.",
    )

    def _rate(self) -> int:
        return int(self.values["rate"]) if self.live() else int(self.values["file_rate"])

    def upstream(self):
        if not self.live():
            return None
        v = self.values
        freq, rate = float(v["centre_mhz"]) * 1e6, int(v["rate"])
        if self.bridge():
            return self.relay_stream(freq, rate)
        if rate > RTL_MAX_RATE:
            raise RuntimeError("An RTL-SDR goes up to 2.4 MS/s here: choose it in Settings → Sample rate.")
        cmd = ["rtl_sdr", "-d", self.device_index().lstrip(":"), "-f", str(int(freq)), "-s", str(rate),
               "-p", str(v["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def command(self):
        v = self.values
        rate = self._rate()
        if rate % 48000:
            raise RuntimeError("The sample rate must be a multiple of 48 kHz.")
        freqs = []
        for f in str(v["freqs"]).replace(";", ",").split(","):
            if f.strip():
                try:
                    freqs.append(float(f) * 1e6)
                except ValueError:
                    raise RuntimeError(f"Frequencies: {f.strip()!r} isn't a number in MHz.") from None
        cmd = system.python_command("sdr_toolkit.engines.navaid") + [
            "--input", "-" if self.live() else self.file_path(), "--rate", str(rate),
            "--centre", str(int(float(v["centre_mhz"]) * 1e6))]
        if freqs:
            cmd += ["--freqs", ",".join(str(int(f)) for f in freqs)]
        self._beacons: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._channels = 0
        return cmd

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            return [Event("line", line.strip(), style="warn")] if "navaid:" in line else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        if e.get("event") == "start":
            self._channels = len(e.get("channels", []))
            return [Event("status", self.activity())]
        if "freq" not in e:
            return []
        key = f"{float(e['freq']) / 1e6:.2f}"
        b = self._beacons.setdefault(key, {"kind": "", "ident": "", "first": time.time()})
        b["last"] = time.time()
        events = []
        if e.get("event") == "ident":
            text = str(e.get("text", "")).replace(" ", "")
            # A partial ident (the decoder joined the stream mid-way) is shorter: keep the longest heard lately.
            if text and (len(text) >= len(b["ident"]) or time.time() - b.get("ident_t", 0) > 120):
                if text != b["ident"]:
                    events.append(Event("log", f"{key} MHz: ident {text} ({b['kind'] or 'beacon'})"))
                b["ident"], b["ident_t"] = text, time.time()
            self._dirty = True
            return events
        if e.get("event") != "navaid":
            return []
        new = b["kind"] != e["kind"] and e["kind"] != "carrier"
        if e["kind"] != "carrier" or not b["kind"]:
            b["kind"] = e["kind"]
        b["data"] = e
        b["reading"] = reading(e)
        if new:
            names = {"VOR": "VOR", "LOC": "ILS localizer", "GS": "ILS glide slope"}
            events.append(Event("line", f"New beacon: {names.get(e['kind'], e['kind'])} on {key} MHz"))
        events.append(Event("log", f"{key} MHz {b['kind']}{' ' + b['ident'] if b['ident'] else ''}: "
                                   f"{b['reading']}", style="dim"))
        self._dirty = True
        return events

    def poll(self):
        events = super().poll()
        now = time.time()
        if getattr(self, "_dirty", False) and now - self._last_table >= 1.0:
            self._last_table = now
            self._dirty = False
            events.append(self._table())
        return events

    def stop(self):
        events = super().stop()
        if getattr(self, "_beacons", None):
            events.append(self._table())
        return events

    def _table(self):
        rows = []
        names = {"VOR": "VOR", "LOC": "ILS localizer", "GS": "ILS glide slope", "carrier": "carrier"}
        for key, b in sorted(getattr(self, "_beacons", {}).items()):
            d = b.get("data", {})
            rows.append({"freq": key, "kind": names.get(b["kind"], b["kind"]), "ident": b["ident"],
                         "reading": b.get("reading", ""), "snr": d.get("snr", ""),
                         "last": time.strftime("%H:%M:%S", time.localtime(b["last"]))})
        return Event("table", data={"rows": rows})

    def details(self, key):
        b = getattr(self, "_beacons", {}).get(key)
        if b is None:
            return None
        d = b.get("data", {})
        f = [("Frequency", f"{key} MHz"), ("Reading", b.get("reading", ""))]
        if b["ident"]:
            f.append(("Ident", b["ident"]))
        if d.get("kind") == "VOR":
            f += [("30 Hz variable (AM)", f"{d['var_depth'] * 100:.0f}% (30% nominal)"),
                  ("9960 Hz reference deviation", f"±{d['ref_dev']:.0f} Hz (±480 nominal)")]
        if d.get("kind") in ("LOC", "GS"):
            f += [("90 Hz depth", f"{d['m90'] * 100:.1f}%"), ("150 Hz depth", f"{d['m150'] * 100:.1f}%"),
                  ("Sum of depths", f"{d['sdm'] * 100:.0f}% ({'40' if d['kind'] == 'LOC' else '80'}% nominal)")]
        mhz = float(key)
        if is_localizer(mhz) and round(mhz, 2) in GS_PAIRS:
            f.append(("Its glide slope", f"{GS_PAIRS[round(mhz, 2)]:.2f} MHz"))
        f += [("Signal", f"{d.get('snr', '?')} dB above the noise, {d.get('level', '?')} dBFS")]
        title = f"{b['ident'] or key} {('VOR' if b['kind'] == 'VOR' else 'ILS') if b['kind'] in ('VOR', 'LOC', 'GS') else ''}"
        return {"title": title.strip(), "subtitle": f"{key} MHz", "fields": f}

    def listening_on(self):
        rate = self._rate() if hasattr(self, "values") else 2400000
        return f"{float(self.values['centre_mhz']):g} MHz ± {rate * 0.425 / 1e6:.1f} MHz"

    def activity(self):
        heard = sum(1 for b in getattr(self, "_beacons", {}).values() if b["kind"] in ("VOR", "LOC", "GS"))
        return super().activity() + f" · {getattr(self, '_channels', 0)} channels watched, {heard} beacons"
