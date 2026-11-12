"""The satellite catalogue: what's up there, its orbit, and what it transmits.

From the SatNOGS DB (db.satnogs.org, the community's database of satellite radios): the
satellites, their transmitters (frequency, mode, baud rate, whether they're still heard) and
current orbital elements. CelesTrak's group lists say which are space stations, weather and
amateur satellites. Everything is cached in ~/.cache/sdr-toolkit/satellites: the elements are
fetched again after 12 hours, the rest after a week, and the cache is used when offline.

Each downlink is sorted into what you can do with it here: listen to it (FM voice, SSB) or decode
it in the toolkit (APRS, SSTV, Morse), with how to tune; or what it needs instead (SatDump for
weather pictures, gr-satellites for telemetry), or that it's outside an RTL-SDR's range.
"""

import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from .. import system

log = logging.getLogger(__name__)
SATNOGS = "https://db.satnogs.org/api/{}/?format=json"
CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP={}&FORMAT=tle"
GROUPS = {"stations": "Space stations", "weather": "Weather", "noaa": "Weather", "amateur": "Amateur"}
FRESH = {"tle": 12 * 3600, "satellites": 7 * 86400, "transmitters": 7 * 86400, "group": 7 * 86400}
RTL_RANGE = (24e6, 1766e6)
CATEGORIES = ("Space stations", "Weather", "Amateur", "Fun to listen to", "Other")
KIND_ORDER = {"decode": 0, "listen": 1, "pictures": 2, "telemetry": 3, "out": 4}


def cache_dir() -> Path:
    return system.cache_dir() / "satellites"


def _fetch(url, path: Path, max_age, offline=False) -> bytes | None:
    """The file at url, from the cache while it's fresh (or when the fetch fails)."""
    fresh = path.is_file() and time.time() - path.stat().st_mtime < max_age
    if fresh or offline:
        return path.read_bytes() if path.is_file() else None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"SDR Toolkit/{__version__}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return data
    except OSError as e:
        log.info("satellites: %s: %s", url, e)
        return path.read_bytes() if path.is_file() else None


@dataclass
class Radio:
    """One transmitter (or transponder) of a satellite, and what you can do with it here."""
    description: str
    downlink: float                 # Hz
    downlink_high: float
    uplink: float | None
    mode: str
    baud: float | None
    alive: bool
    kind: str                       # listen, decode, pictures, telemetry, out (of RTL-SDR range)
    how: str                        # in words
    demod: str = ""                 # Spectrum & receiver settings to hear it
    bandwidth: int = 0
    decoder: str = ""
    service: str = ""
    invert: bool = False


def short(radio) -> str:
    """A downlink in a few words: "145.825 APRS", "145.800 FM voice"."""
    what = {"aprs": "APRS", "sstv": "SSTV", "morse": "CW"}.get(radio.decoder) or (
        "FM voice" if radio.kind == "listen" and radio.mode in ("FM", "FMN") else radio.mode)
    return f"{radio.downlink / 1e6:.3f} {what}"


@dataclass
class Satellite:
    norad: int
    name: str
    names: str = ""
    line1: str = ""
    line2: str = ""
    categories: list = field(default_factory=list)
    radios: list = field(default_factory=list)
    operator: str = ""
    countries: str = ""
    launched: str = ""
    website: str = ""
    image: str = ""

    @property
    def can_hear(self) -> bool:
        """A downlink an RTL-SDR can receive that's still heard."""
        return any(r.alive and r.kind != "out" for r in self.radios)

    @property
    def fun(self) -> bool:
        return any(r.alive and r.kind in ("listen", "decode") for r in self.radios)


def classify(mode, baud, freq, description="") -> tuple:
    """(kind, how, demod, bandwidth, decoder) for a downlink."""
    m = (mode or "").upper()
    d = (description or "").upper()
    if not freq or not RTL_RANGE[0] <= freq <= RTL_RANGE[1]:
        return "out", "outside an RTL-SDR's range (24 MHz to 1.77 GHz)", "", 0, ""
    if m == "SSTV" or ("SSTV" in d and m in ("FM", "FMN")):
        return "decode", "SSTV pictures: FM, decode with SSTV", "NFM", 12500, "sstv"
    if (m == "AFSK" and (not baud or baud == 1200)) or ("APRS" in d and m in ("FM", "AFSK")):
        return "decode", "APRS / packet (1200 baud AFSK over FM): decode with APRS", "NFM", 12500, "aprs"
    if m in ("FM", "FMN") and not baud:
        return "listen", "FM voice: listen in NFM", "NFM", 12500 if m == "FMN" else 15000, ""
    if m == "CW":
        return "decode", "Morse beacon: decode with Morse", "CW", 500, "morse"
    if m in ("USB", "LSB"):
        return "listen", f"SSB (a linear transponder or beacon): listen in {m}", m, 2700, ""
    if m == "APT":
        return "pictures", "APT weather pictures: NFM 34 kHz, decode with an APT decoder (not in the toolkit)", \
            "NFM", 34000, ""
    if m in ("LRPT", "HRPT", "AHRPT"):
        dish = " (needs a dish)" if freq > 1e9 else ""
        return "pictures", f"{m} weather pictures{dish}: decode with SatDump (not in the toolkit)", "", 0, ""
    if m == "LORA":
        return "telemetry", "LoRa telemetry", "", 0, ""
    rate = f" {baud:g} baud" if baud else ""
    return "telemetry", f"{mode or 'unknown'}{rate} telemetry: decode with gr-satellites or SatNOGS", "", 0, ""


def _norads(tle_text: str) -> set:
    out = set()
    for line in tle_text.splitlines():
        if line.startswith("1 ") and len(line) > 7:
            try:
                out.add(int(line[2:7]))
            except ValueError:
                pass
    return out


def load(offline=False, folder: Path | None = None) -> dict[int, Satellite]:
    """The catalogue, by NORAD number: satellites in orbit with current elements."""
    folder = folder or cache_dir()
    raw = {}
    for name in ("satellites", "transmitters", "tle"):
        data = _fetch(SATNOGS.format(name), folder / f"satnogs_{name}.json", FRESH[name], offline)
        try:
            raw[name] = json.loads(data) if data else []
        except ValueError:
            raw[name] = []
    groups: dict[int, list] = {}
    for g, label in GROUPS.items():
        text = _fetch(CELESTRAK.format(g), folder / f"celestrak_{g}.txt", FRESH["group"], offline)
        for n in _norads(text.decode(errors="replace") if text else ""):
            groups.setdefault(n, [])
            if label not in groups[n]:
                groups[n].append(label)
    tles = {t["norad_cat_id"]: t for t in raw["tle"] if t.get("tle1") and t.get("tle2")}
    sats: dict[int, Satellite] = {}
    for s in raw["satellites"]:
        n = s.get("norad_cat_id")
        if n is None or s.get("status") != "in orbit" or n not in tles:
            continue
        t = tles[n]
        name = (t.get("tle0") or "").removeprefix("0 ").strip() or s.get("name") or str(n)
        sats[n] = Satellite(n, s.get("name") or name, s.get("names") or "", t["tle1"], t["tle2"],
                            list(groups.get(n, [])), [], s.get("operator") or "", s.get("countries") or "",
                            (s.get("launched") or "")[:10], s.get("website") or "",
                            f"https://db-satnogs.freetls.fastly.net/media/{s['image']}" if s.get("image") else "")
    for tx in raw["transmitters"]:
        sat = sats.get(tx.get("norad_cat_id"))
        if sat is None or not tx.get("downlink_low") or tx.get("type") not in ("Transmitter", "Transceiver",
                                                                                 "Transponder"):
            continue
        f = float(tx["downlink_low"])
        kind, how, demod, bw, decoder = classify(tx.get("mode"), tx.get("baud"), f, tx.get("description"))
        sat.radios.append(Radio(tx.get("description") or "", f, float(tx.get("downlink_high") or f),
                                float(tx["uplink_low"]) if tx.get("uplink_low") else None, tx.get("mode") or "",
                                tx.get("baud"), bool(tx.get("alive")) and tx.get("status") == "active", kind, how,
                                demod, bw, decoder, tx.get("service") or "", bool(tx.get("invert"))))
    for sat in sats.values():
        # Heard ones first; then what you can do with them, the amateur ones before the rest.
        sat.radios.sort(key=lambda r: (not r.alive, KIND_ORDER.get(r.kind, 5), r.service != "Amateur", r.downlink))
        if "Weather" not in sat.categories and any(r.service == "Meteorological" or r.kind == "pictures"
                                                   for r in sat.radios):
            sat.categories.append("Weather")
        if "Amateur" not in sat.categories and any(r.service == "Amateur" for r in sat.radios):
            sat.categories.append("Amateur")
        if sat.fun:
            sat.categories.append("Fun to listen to")
        if not sat.categories:
            sat.categories.append("Other")
    return sats
