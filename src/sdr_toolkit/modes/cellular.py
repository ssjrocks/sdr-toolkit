"""LTE cells around: each tower cell on air in the bands scanned, its channel (EARFCN), band, physical cell ID
(PCI), FDD or TDD, and signal, from its synchronisation signals (engines/lte.py). The SDR steps across the bands
(16 MHz at a time on a HackRF, 1.6 MHz on an RTL-SDR)."""

import json
import time

from .. import system
from ..engines.lte import BANDS
from .base import Event, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode

PRESETS = {
    "au": ("Australia (28, 5, 8, 3, 1, 7, 40)", "28,5,8,3,1,7,40"),
    "na": ("North America (12, 13, 71, 5, 2, 4, 66, 41)", "12,13,71,5,2,4,66,41"),
    "eu": ("Europe (20, 8, 3, 1, 7)", "20,8,3,1,7"),
    "low": ("Below 1 GHz only (an RTL-SDR: 28, 5, 8, 20, 12, 13, 71)", "28,5,8,20,12,13,71"),
}
RTL_REACH = 1_766e6


class CellularMode(RadioMode):
    id = "cellular"
    name = "LTE cells (mobile towers)"
    group = "SDR"
    summary = ("The LTE cells of the mobile towers around: each cell's channel, band, physical cell ID, FDD or "
               "TDD, and signal strength, from the synchronisation signals they all send. Receive only.")
    output = "table"
    requires = ("rtl-sdr",)
    key_column = "key"
    columns = (("freq", "MHz"), ("band", "Band"), ("earfcn", "EARFCN"), ("pci", "PCI"), ("duplex", "Duplex"),
               ("power", "Level dBFS"), ("snr", "Sync dB"), ("count", "Seen"), ("last", "Last seen"), ("key", "ID"))
    settings = RADIO_SETTINGS + (
        Setting("bands", "Bands", "au", "choice", restart=True, choices=tuple((k, v[0]) for k, v in PRESETS.items())),
        Setting("file_rate", "Recording sample rate", 19_200_000, "int", 1_920_000, 19_200_000, 1_920_000,
                suffix=" Hz", restart=True, help="A multiple of 1.92 MS/s."),
        Setting("file_centre_mhz", "Recording centre frequency", 790.0, "float", 400.0, 6000.0, 0.1, 1,
                suffix=" MHz", restart=True),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ: 16 MHz windows (HackRF) or 1.6 MHz (RTL-SDR) stepped across the LTE bands",
        bandwidth="Every cell sends its sync signals in its middle 1.08 MHz, whatever its width",
        frequencies=tuple((f"Band {b}", f"{lo}-{hi} MHz downlink") for b, (lo, hi, _) in BANDS.items()),
        notes="A HackRF covers every band; an RTL-SDR reaches the bands below 1.7 GHz. A PCI is shown when the "
              "cell's secondary sync signal stands out clearly (else only its group, 0-2, is certain). Which "
              "operator a cell belongs to isn't sent in the sync signals.",
    )

    def _hackrf(self):
        b = self.bridge()
        return b is not None and b.device.get("type") == "hackrf"

    def _rate(self):
        if not self.live():
            return int(self.values["file_rate"])
        return 19_200_000 if self._hackrf() else 1_920_000

    def _bands(self):
        bands = [int(b) for b in PRESETS[self.values["bands"]][1].split(",")]
        if self.live() and not self._hackrf():
            bands = [b for b in bands if BANDS[b][1] * 1e6 < RTL_REACH]
        return bands

    def listening_on(self):
        return "bands " + ", ".join(str(b) for b in self._bands())

    def command(self):
        self._cells: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._window = None
        cmd = system.python_command("sdr_toolkit.engines.lte") + ["--rate", str(self._rate())]
        if not self.live():
            return cmd + ["--input", self.file_path(), "--centre", str(float(self.values["file_centre_mhz"]) * 1e6)]
        bands = self._bands()
        if not bands:
            raise RuntimeError("None of these bands is in an RTL-SDR's reach: choose 'Below 1 GHz', or use a HackRF.")
        b = self.bridge()
        self._port = b.local_port if b else self._free_port()
        cmd += ["--port", str(self._port), "--bands", ",".join(str(x) for x in bands)]
        if self.gain_db():
            cmd += ["--gain", self.gain_db()]
        if b:
            return cmd
        self._start_rtl_tcp()
        return self._wait_for_rtl_tcp(cmd)

    def stop(self):
        events = super().stop()
        self._stop_rtl_tcp()
        if getattr(self, "_cells", None):
            events.append(self._table())
        return events

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            return [Event("line", line.strip(), style="warn")] if "lte:" in line else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        if e.get("event") == "window":
            self._window = e
            return [Event("status", self.activity())]
        if e.get("event") != "cell":
            return []
        # One cell shows up at a few raster points (a Zadoff-Chu PSS also matches shifted in frequency): the same
        # PCI within 1.5 MHz, or an unsure one near any cell, is that cell, kept at its strongest frequency.
        near = [k for k, c in self._cells.items() if abs(c["freq"] - e["freq"]) < 1.5e6
                and (c["pci"] == e["pci"] or e["pci"] is None or c["pci"] is None)]
        if near:
            k0 = max(near, key=lambda k: self._cells[k]["pss_snr"])
            c0 = self._cells[k0]
            if e["pci"] is None and c0["pci"] is not None or e["pss_snr"] <= c0["pss_snr"]:
                c0["count"] += 1
                c0["last"] = time.time()
                self._dirty = True
                return []
            del self._cells[k0]                                    # the stronger reading replaces it
            e = dict(e, count0=c0["count"])
        key = f"{e['earfcn']}/{e['pci'] if e['pci'] is not None else 'N_ID2 ' + str(e['n_id2'])}"
        new = key not in self._cells and "count0" not in e
        c = self._cells.setdefault(key, {"count": e.pop("count0", 0), "first": time.time()})
        c.update(e)
        c["count"] += 1
        c["last"] = time.time()
        self._dirty = True
        if new:
            pci = e["pci"] if e["pci"] is not None else f"? (group {e['n_id2']})"
            return [Event("log", f"LTE cell: {e['freq'] / 1e6:.1f} MHz, band {e['band']} (EARFCN {e['earfcn']}), "
                                 f"PCI {pci}{', ' + e['duplex'] if e['duplex'] else ''}, sync {e['pss_snr']:.0f} dB")]
        return []

    def poll(self):
        events = super().poll()
        if getattr(self, "_dirty", False) and time.time() - self._last_table >= 1.0:
            self._dirty = False
            self._last_table = time.time()
            events.append(self._table())
        return events

    def _table(self):
        rows = []
        for key, c in sorted(self._cells.items(), key=lambda kv: kv[1]["freq"]):
            rows.append({"key": key, "freq": f"{c['freq'] / 1e6:.1f}", "band": c["band"], "earfcn": c["earfcn"],
                         "pci": c["pci"] if c["pci"] is not None else f"? ({c['n_id2']})", "duplex": c["duplex"],
                         "power": c["power"], "snr": c["pss_snr"], "count": c["count"],
                         "last": time.strftime("%H:%M:%S", time.localtime(c["last"]))})
        return Event("table", data={"rows": rows})

    def details(self, key):
        c = getattr(self, "_cells", {}).get(key)
        if c is None:
            return None
        f = [("Frequency", f"{c['freq'] / 1e6:.1f} MHz"), ("Band", str(c["band"])), ("EARFCN", str(c["earfcn"])),
             ("Physical cell ID", str(c["pci"]) if c["pci"] is not None else "not certain"),
             ("Group (N_ID1 / N_ID2)", f"{c['n_id1'] if c['n_id1'] is not None else '?'} / {c['n_id2']}"),
             ("Duplex", c["duplex"] or "not certain"), ("Primary sync", f"{c['pss_snr']} dB above the noise"),
             ("Secondary sync match", f"{c['sss_score']}× the median"), ("Level", f"{c['power']} dBFS"),
             ("Crystal offset seen", f"{c['freq_offset']} Hz"), ("Times seen", str(c["count"]))]
        return {"title": f"Cell {c['pci'] if c['pci'] is not None else '?'} on {c['freq'] / 1e6:.1f} MHz",
                "subtitle": f"Band {c['band']}", "fields": f}

    def activity(self):
        w = getattr(self, "_window", None)
        where = f" · now {w['lo'] / 1e6:.0f}-{w['hi'] / 1e6:.0f} MHz" if w else ""
        return super().activity() + f" · {len(getattr(self, '_cells', {}))} cells{where}"
