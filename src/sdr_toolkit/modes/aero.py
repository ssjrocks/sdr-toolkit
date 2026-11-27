"""Aircraft messages: ACARS (VHF, acarsdec), VDL Mode 2 (dumpvdl2) and HFDL (HF, dumphfdl), the text and data
aircraft and ground stations exchange: positions, weather, flight plans, engine reports, free text. All three
read the SDR's raw IQ from a pipe (rtl_sdr, or the relay bridge) or a recording, several channels at once.

One row per aircraft, by its ICAO address (or registration or flight when that's all a message gives), with its
latest message; positions (HFDL and some VDL2 / ACARS reports carry them) go on the map. Each message is logged
with the aircraft's ICAO address, so ADS-B's Details tab can show an aircraft's messages too."""

import collections
import json
import math
import re
import time

from .. import system
from .base import Event, MissingParts, Setting, Tuning
from .process import looks_like_error
from .radio import LOCATION_SETTINGS, RADIO_SETTINGS, RadioMode, home, rtl_rate_ok

ACARS_CHANNELS = "130.025 130.425 130.450 131.125 131.450 131.475 131.525 131.550 131.725 131.825"
VDL2_CHANNELS = "136.650 136.700 136.725 136.775 136.800 136.825 136.875 136.900 136.925 136.975"
# HFDL ground stations' frequencies (kHz), by band.
HFDL_BANDS = {
    "21": "21928 21931 21934 21937 21949 21955 21982 21990 21997",
    "17": "17901 17912 17916 17919 17922 17928 17934 17958 17967 17985",
    "13": "13264 13270 13276 13303 13312 13315 13321 13324 13342 13351 13354",
    "11": "11184 11306 11312 11318 11321 11327 11348 11354 11384 11387",
    "10": "10027 10030 10060 10063 10066 10075 10081 10084 10087 10093",
    "8": "8825 8834 8843 8885 8886 8894 8912 8921 8927 8936 8939 8942 8948 8957 8977",
    "6": "6529 6532 6535 6559 6565 6589 6619 6628 6646 6652 6661 6712",
    "5": "5451 5502 5508 5514 5529 5538 5544 5547 5583 5589 5622 5652 5655 5720",
    "4": "4654 4660 4681 4687",
    "3": "3455 3497",
    "2": "2941 2944 2992 2998 3007 3016",
}
HFDL_RATE = 960_000
DUPLICATE_S = 5.0


def _mhz(freq) -> str:
    """A decoder's frequency (acarsdec: MHz; dumpvdl2 and dumphfdl: Hz or kHz) as text in MHz."""
    try:
        f = float(freq)
    except (TypeError, ValueError):
        return ""
    if f >= 1e5:
        f /= 1e6                                          # Hz
    elif f >= 1e3:
        f /= 1e3                                          # kHz
    return f"{f:.3f} MHz"


def tool(name) -> str | None:
    p = system.data_dir() / "aero" / "bin" / name
    return str(p) if p.is_file() else None


def find(d, keys):
    """The first value under any of *keys*, anywhere in a decoder's JSON."""
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        for v in d.values():
            r = find(v, keys)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = find(v, keys)
            if r is not None:
                return r
    return None


def aircraft_address(d):
    """An aircraft's ICAO address: a source or destination of type Aircraft (VDL2's AVLC, HFDL's LPDU), or an
    "icao" field."""
    if isinstance(d, dict):
        if str(d.get("type", "")).lower() == "aircraft" and d.get("addr"):
            return str(d["addr"]).upper()
        if d.get("icao"):
            return str(d["icao"]).upper()
        for v in d.values():
            r = aircraft_address(v)
            if r:
                return r
    elif isinstance(d, list):
        for v in d:
            r = aircraft_address(v)
            if r:
                return r
    return None


def position(d):
    """A position in a message: a dict with numeric lat and lon (HFDL's position reports, ADS-C, some ACARS)."""
    if isinstance(d, dict):
        lat, lon = d.get("lat"), d.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and abs(lat) <= 90 and abs(lon) <= 180 \
                and (lat or lon):
            return float(lat), float(lon)
        for v in d.values():
            r = position(v)
            if r:
                return r
    elif isinstance(d, list):
        for v in d:
            r = position(v)
            if r:
                return r
    return None


class AeroMode(RadioMode):
    id = "aero"
    name = "Aircraft messages (ACARS, VDL2, HFDL)"
    group = "SDR"
    summary = ("The messages aircraft and ground stations exchange: ACARS on VHF, VDL Mode 2 and HFDL on HF. "
               "Positions, weather, flight plans, engine reports and free text, one row per aircraft. Decoded by "
               "acarsdec, dumpvdl2 and dumphfdl.")
    output = "table"
    map = True
    columns = (("key", "Aircraft"), ("reg", "Reg"), ("flight", "Flight"), ("count", "Messages"),
               ("last_msg", "Latest message"), ("system", "Heard on"), ("last", "Last heard"))
    key_column = "key"
    requires = ("aero",)
    settings = RADIO_SETTINGS + LOCATION_SETTINGS + (
        Setting("system", "Listen to", "acars", "choice", restart=True,
                choices=(("acars", "ACARS (VHF, 129-132 MHz)"), ("vdl2", "VDL Mode 2 (VHF, 136-137 MHz)"),
                         ("hfdl", "HFDL (HF, 2-22 MHz)"))),
        Setting("acars_channels", "ACARS channels", ACARS_CHANNELS, "text", restart=True,
                help="MHz, space-separated, within 2.2 MHz on an RTL-SDR (more on a HackRF). 131.550 is used "
                     "worldwide; 131.725 / 131.525 in Europe, 130.025 / 130.450 / 131.125 in North America."),
        Setting("vdl2_channels", "VDL2 channels", VDL2_CHANNELS, "text", restart=True,
                help="MHz, space-separated. 136.975 is the common channel worldwide."),
        Setting("hfdl_band", "HFDL band", "8", "choice", restart=True,
                choices=tuple((k, f"{k} MHz: {v.split()[0]}-{v.split()[-1]} kHz") for k, v in HFDL_BANDS.items()),
                help="Which ground stations' frequencies to listen to. Daytime favours 13-21 MHz, night 5-10 MHz. "
                     "An RTL-SDR needs HF reception (a V4, or an upconverter); a HackRF tunes there itself."),
        Setting("direct", "HF on an RTL-SDR (HFDL)", 0, "choice", restart=True,
                choices=((0, "Tuner (a V4, or an upconverter)"), (2, "Direct sampling, Q branch (Blog V3)"),
                         (1, "Direct sampling, I branch")),
                help="How a dongle on this PC reaches HF for HFDL. A HackRF tunes there itself.", sdr="rtl"),
        Setting("file_rate", "Recording sample rate", 2400000, "int", 48000, 20000000, 1000, suffix=" Hz",
                restart=True),
        Setting("file_centre_mhz", "Recording centre frequency", 130.925, "float", 0.0, 6000.0, 0.001, 4,
                suffix=" MHz", restart=True),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the SDR (the toolkit tunes it): several channels at once",
        bandwidth="ACARS: 2.4 MS/s around the channels; VDL2: 2.1 MS/s; HFDL: 960 kS/s",
        frequencies=(("131.550 MHz", "ACARS, worldwide"), ("129-132 MHz", "ACARS (the region's channels)"),
                     ("136.975 MHz", "VDL Mode 2, worldwide"), ("136.6-137 MHz", "VDL Mode 2"),
                     ("2.9-22 MHz", "HFDL: ground stations around the world, heard at night on 5-10 MHz")),
        notes="An aircraft's messages come every few minutes, more around airports. A VHF airband antenna (or a "
              "120 MHz quarter-wave, 60 cm) helps; HFDL wants a long wire. Many ACARS messages are libacars-decoded "
              "(ADS-C positions, CPDLC, flight plans).",
    )

    # ---- what runs ---------------------------------------------------------------------------------

    def plan(self) -> tuple[float, int, list[float]]:
        """(centre Hz, sample rate, channels Hz) for the chosen system."""
        v = self.values
        if v["system"] == "hfdl":
            chans = [float(x) * 1e3 for x in HFDL_BANDS[v["hfdl_band"]].split()]
            return (min(chans) + max(chans)) / 2, HFDL_RATE, chans
        if v["system"] == "vdl2":
            chans = [float(x) * 1e6 for x in v["vdl2_channels"].split()]
            span = max(chans) - min(chans)
            k = max(10, math.ceil(span / 0.8 / 105000))       # dumpvdl2: a multiple of 105 kS/s
            while not rtl_rate_ok(k * 105000) and k < 30:
                k += 1
            return (min(chans) + max(chans)) / 2, k * 105000, chans
        chans = [float(x) * 1e6 for x in v["acars_channels"].split()]
        span = max(chans) - min(chans)
        rate = max(2_400_000, math.ceil(span / 0.8 / 240000) * 240000)    # a multiple of 12 kS/s for acarsdec
        return (min(chans) + max(chans)) / 2, rate, chans

    def upstream(self):
        if not self.live():
            return None
        centre, rate, _ = self.plan()
        if self.bridge():
            return self.relay_stream(centre, rate)
        if rate > 3_200_000:
            raise RuntimeError("The channels are too far apart for an RTL-SDR (it sees 2.2 MHz at once): "
                               "remove some, or use a HackRF.")
        cmd = ["rtl_sdr", "-d", self.device_index().lstrip(":"), "-f", str(int(centre)), "-s", str(rate),
               "-p", str(self.values["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        if self.values["system"] == "hfdl" and int(self.values.get("direct", 0)):
            cmd += ["-D", str(int(self.values["direct"]))]
        return cmd + ["-"]

    def command(self):
        v = self.values
        self._reset()
        system_ = v["system"]
        exe = tool({"acars": "acarsdec", "vdl2": "dumpvdl2", "hfdl": "dumphfdl"}[system_])
        if exe is None:
            raise MissingParts(["aero"])
        centre, rate, chans = self.plan()
        src = "-"
        if not self.live():
            src = self.file_path()
            rate = int(v["file_rate"])
            centre = float(v["file_centre_mhz"]) * 1e6
        if system_ == "acars":
            return [exe, "--output", "json:file:path=-", "--iqfile",
                    f"file={'/dev/stdin' if src == '-' else src},sr={rate}", "-c", f"{centre / 1e6:.4f}",
                    *[f"{c / 1e6:.3f}" for c in chans]]
        if system_ == "vdl2":
            return [exe, "--iq-file", src, "--sample-format", "U8", "--oversample", str(rate // 105000),
                    "--centerfreq", str(int(centre)), *[str(int(c)) for c in chans],
                    "--output", "decoded:json:file:path=-"]
        return [exe, "--iq-file", src, "--sample-format", "CU8", "--sample-rate", str(rate),
                "--read-buffer-size", "96000", "--centerfreq", f"{centre / 1e3:.1f}",
                *[f"{c / 1e3:.1f}" for c in chans], "--output", "decoded:json:file:path=-"]

    # ---- what it hears ----------------------------------------------------------------------------

    def _reset(self):
        self._aircraft: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._messages = 0
        self._recent: dict[tuple, tuple[float, str, float]] = {}

    def parse(self, line):
        if not hasattr(self, "_aircraft"):
            self._reset()
        line = line.strip()
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            if looks_like_error(line):
                return [Event("status", line), Event("line", line, style="warn")]
            return []
        try:
            d = json.loads(line)
        except ValueError:
            return []
        return self._message(d)

    def _message(self, d):
        now = time.time()
        system_ = "VDL2" if "vdl2" in d else "HFDL" if "hfdl" in d else "ACARS"
        icao = aircraft_address(d)
        reg = str(find(d, ("reg", "tail")) or "").strip(".").strip()
        flight = str(find(d, ("flight", "flight_id")) or "").strip()
        label = str(find(d, ("label",)) or "")
        text = str(find(d, ("msg_text", "text")) or "").strip()
        pos = position(d)
        key = icao or reg or flight
        if not key:
            return []                                     # a ground station's housekeeping, no aircraft in it
        freq = find(d, ("freq",))
        level = find(d, ("level", "sig_level"))
        level = float(level) if isinstance(level, (int, float)) else -999.0
        # A strong aircraft is also decoded on the channels next to its own (acarsdec's filters let ~30 dB
        # through): the same message within a few seconds counts once, at the channel that heard it loudest.
        sig = (system_, key, label, str(find(d, ("block_id", "blk_id", "msgno")) or ""), text)
        self._recent = {k: v for k, v in self._recent.items() if now - v[0] < DUPLICATE_S}
        if sig in self._recent:
            _, k0, best = self._recent[sig]
            if level > best and k0 in self._aircraft:
                self._recent[sig] = (now, k0, level)
                self._aircraft[k0]["freq"] = _mhz(freq)
            return []
        self._recent[sig] = (now, key, level)
        self._messages += 1
        a = self._aircraft.setdefault(key, {"first": now, "count": 0, "msgs": collections.deque(maxlen=50),
                                            "systems": set()})
        a["count"] += 1
        a["last"] = now
        a["seq"] = self._messages                         # newest first, even within one clock tick
        a["systems"].add(system_)
        for k, val in (("icao", icao), ("reg", reg), ("flight", flight)):
            if val:
                a[k] = val
        summary = " ".join(x for x in (f"[{label}]" if label else "", text.replace("\n", " ")[:160]) if x)
        a["msgs"].append((now, system_, summary))
        if summary:
            a["last_msg"] = summary
        if pos:
            a["pos"] = pos
        if freq is not None:
            a["freq"] = _mhz(freq)
        self._dirty = True
        ident = " ".join(x for x in (f"ICAO {icao}" if icao else "", f"Reg {reg}" if reg else "",
                                     f"Flight {flight}" if flight else "") if x)
        events = [Event("log", f"{system_} {ident}: {summary or '(no text)'}")]
        if pos:
            events.append(Event("log", f"{system_} {ident}: position {pos[0]:.4f}, {pos[1]:.4f}"))
        return events

    def poll(self):
        events = super().poll()
        now = time.time()
        if getattr(self, "_dirty", False) and now - self._last_table >= 1.0:
            self._last_table = now
            self._dirty = False
            events += self._views()
        return events

    def stop(self):
        events = super().stop()
        if getattr(self, "_aircraft", None):
            events += self._views()
        return events

    def _views(self):
        rows = []
        points = []
        for key, a in sorted(self._aircraft.items(), key=lambda kv: (-kv[1]["last"], -kv[1]["seq"])):
            rows.append({"key": key, "reg": a.get("reg", ""), "flight": a.get("flight", ""), "count": a["count"],
                         "last_msg": a.get("last_msg", ""), "system": ", ".join(sorted(a["systems"])),
                         "last": time.strftime("%H:%M:%S", time.localtime(a["last"]))})
            if "pos" in a:
                points.append({"id": key, "lat": a["pos"][0], "lon": a["pos"][1], "kind": "aircraft",
                               "label": a.get("flight") or a.get("reg") or key})
        return [Event("table", data={"rows": rows}), Event("points", data={"points": points, "home": home(self.values)})]

    def details(self, key):
        a = getattr(self, "_aircraft", {}).get(key)
        if a is None:
            return None
        f = [(k.upper() if k == "icao" else k.capitalize(), a[k]) for k in ("icao", "reg", "flight") if a.get(k)]
        f += [("Messages", str(a["count"])), ("Heard on", ", ".join(sorted(a["systems"])))]
        if a.get("freq"):
            f.append(("Frequency", a["freq"]))
        if "pos" in a:
            f.append(("Last position", f"{a['pos'][0]:.4f}, {a['pos'][1]:.4f}"))
        for t, sys_, msg in list(a["msgs"])[-8:][::-1]:
            f.append((time.strftime("%H:%M:%S", time.localtime(t)) + f" {sys_}", msg or "(no text)"))
        return {"title": a.get("flight") or a.get("reg") or key, "subtitle": " · ".join(
            x for x in (a.get("reg"), f"ICAO {a['icao']}" if a.get("icao") else "") if x), "fields": f}

    def listening_on(self):
        centre, rate, chans = self.plan()
        unit = 1e3 if self.values["system"] == "hfdl" else 1e6
        names = {"acars": "ACARS", "vdl2": "VDL2", "hfdl": "HFDL"}
        return (f"{len(chans)} {names[self.values['system']]} channels around {centre / unit:g} "
                f"{'kHz' if unit == 1e3 else 'MHz'}")

    def activity(self):
        n = len(getattr(self, "_aircraft", {}))
        return super().activity() + f" · {n} aircraft, {getattr(self, '_messages', 0)} messages"


def logged_messages(logbook, icao="", reg="", limit=8) -> list[tuple[str, str]]:
    """An aircraft's logged ACARS / VDL2 / HFDL messages (by ICAO address, or registration: plain ACARS gives
    only that) as Details rows, newest first: for ADS-B's Details tab."""
    seen, rows = set(), []
    for search in (f"ICAO {icao.upper()}" if icao else "", f"Reg {reg.upper()}" if reg else ""):
        if not search:
            continue
        exact = re.compile(re.escape(search) + r"[ :]")
        for t, _, text in logbook.entries(mode="aero", search=search, limit=limit * 4):
            if exact.search(text.split(": ", 1)[0] + ":") and (t, text) not in seen:
                seen.add((t, text))
                rows.append((t, text))
    rows.sort(key=lambda r: -r[0])
    out = []
    for t, text in rows[:limit]:
        system_, _, rest = text.partition(" ")
        msg = rest.split(": ", 1)[-1]
        out.append((time.strftime("%d %b %H:%M", time.localtime(t)) + f" {system_}", msg))
    return out
