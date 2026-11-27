"""ISM-band devices (weather sensors, tyre sensors, doorbells, remotes, meters...), decoded by rtl_433, on
every band at once where it can be: hopping between them on an RTL-SDR, and on a HackRF each band whole
(engines/ism_wide.py runs rtl_433 on every channel of it).

The table is one row per device, kept up to date: what it is, its latest readings, where, how often it
sends and how strongly. Each message also goes to the decoded log. With the analyser on, signals that
no decoder names are listed too, grouped by modulation and timing, with the rtl_433 decoder spec that
would read them; one can be added as a decoder of your own."""

import collections
import json
import re
import statistics
import time

from .. import system
from .base import Event, Setting, Tuning
from .process import looks_like_error
from .radio import RADIO_SETTINGS, RadioMode

# Fields that describe the message rather than the reading.
META = ("time", "model", "id", "channel", "mod", "freq", "freq1", "freq2", "rssi", "snr", "noise", "protocol",
        "battery_ok", "mic")
# Bands, in Hz.
BANDS = {"315": (314.5e6, 315.5e6), "345": (344.5e6, 345.5e6), "433": (433.05e6, 434.79e6),
         "868": (863e6, 870e6), "915us": (902e6, 928e6), "915au": (915e6, 928e6)}
# A choice of where to listen: (label, bands on a HackRF, frequencies an RTL-SDR hops between).
PRESETS = {
    "all": ("Every band: 315, 345, 433.92, 868 and 902-928 MHz", ("433", "315", "345", "868", "915us"),
            ("433.92M", "315M", "345M", "868.3M", "915M")),
    "au": ("Australia / NZ: 433.92 and 915-928 MHz", ("433", "915au"),
           ("433.92M", "916M", "918M", "920M", "922M", "924M", "926M")),
    "na": ("North America: 315, 345, 433.92 and 902-928 MHz", ("315", "345", "433", "915us"),
           ("315M", "345M", "433.92M", "915M")),
    "eu": ("Europe: 433.92 and 868 MHz", ("433", "868"), ("433.92M", "868.3M", "868.95M")),
}
UNKNOWN_QUIET = 1.2          # s: an analysed burst with no decoded message this close is an unknown signal
UNKNOWN_SEEN = 2             # an unknown signal is listed once heard this often: one-offs are mostly fragments of
                             # other bursts the analyser read as odd pulse codes


class ISMMode(RadioMode):
    id = "ism"
    name = "ISM sensors (rtl_433)"
    group = "SDR"
    summary = ("Weather stations, thermometers, tyre-pressure sensors, doorbells, remotes, alarms and meters on "
               "315/345/433/868/915 MHz: over 250 device types, one row per device. On a HackRF whole bands at "
               "once. Signals nothing decodes are listed too. Decoded by rtl_433.")
    output = "table"
    columns = (("key", "Device"), ("readings", "Latest readings"), ("freq", "MHz"), ("count", "Heard"),
               ("every", "Sends every"), ("signal", "Signal"), ("last", "Last heard"), ("first", "First heard"))
    key_column = "key"
    requires = ("rtl-433",)
    iq_rate = 250000
    settings = RADIO_SETTINGS + (
        Setting("freq", "Where to listen", "all", "choice", restart=True,
                choices=tuple((k, v[0]) for k, v in PRESETS.items())
                + (("433.92M", "433.92 MHz only"), ("915M", "915 MHz only"), ("868.3M", "868.3 MHz only"),
                   ("315M", "315 MHz only"), ("345M", "345 MHz only"), ("custom", "Other (below)")),
                help="A region's bands, or every one. An RTL-SDR hops between them (see Hop every); a HackRF "
                     "listens to each band whole, every channel at once."),
        Setting("custom_freq", "Other frequency", "433.92M", "text", restart=True,
                help="With 'Other': e.g. 434.1M, or several separated by spaces to hop between them."),
        Setting("hop", "Hop every", 60, "int", 10, 3600, 10, suffix=" s", restart=True,
                help="With several bands: how long to stay on each. Sensors send every 30-60 s."),
        Setting("wide", "Whole bands at once", True, "bool", restart=True, sdr="hackrf",
                help="Every channel of a band (up to 17 MHz) decoded at the same time, by rtl_433s of their own. "
                     "Off: one 250 kHz / 1 MHz channel hopping, as on an RTL-SDR. Over a relay this needs a fast "
                     "network: a 13 MHz band is 38 MB/s."),
        Setting("rate", "Sample rate", "250k", "choice", restart=True,
                choices=(("250k", "250 kHz (most devices)"), ("1024k", "1.024 MHz (wide FSK devices)")),
                help="One channel's width (not the whole-band mode). Also the rate a recording must have been "
                     "made at.", per_sdr=True),
        Setting("analyse", "List signals nothing decodes", True, "bool", restart=True,
                help="rtl_433's pulse analyser looks at every burst; one no decoder names is listed as Unknown, "
                     "with its modulation, timings and the decoder spec that would read it."),
        Setting("protocols", "Only protocols", "", "text", restart=True,
                help="rtl_433 protocol numbers, space-separated (e.g. '19 73'); empty decodes all "
                     "default ones. 'rtl_433 -R help' lists them."),
        Setting("flex", "Your decoders", "", "text", restart=True,
                help="rtl_433 flex decoder specs (-X), separated by ';'. 'Add as a decoder' above fills it in "
                     "from an unknown signal; give it a name by changing n=."),
        Setting("level", "Show signal level", True, "bool", help="RSSI and SNR of each device."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the SDR (the toolkit tunes it). A WebSDR can't be used: these need the radio samples.",
        bandwidth="250 kHz or 1 MHz a channel on an RTL-SDR; whole bands (up to 17 MHz) on a HackRF",
        frequencies=(("433.92 MHz", "Weather sensors, remotes, doorbells, tyre sensors (worldwide)"),
                     ("915-928 MHz", "Sensors and meters in Australia/NZ"),
                     ("902-928 MHz", "Sensors and meters in the Americas"),
                     ("868 MHz", "Europe"), ("315 MHz", "North America: tyre sensors, car remotes"),
                     ("345 MHz", "Security sensors (North America)")),
        notes="Sensors transmit every 30-60 s, so give each band a minute. The antenna that came with the "
              "dongle works for nearby devices; a 433 MHz quarter-wave (17 cm) reaches the street.",
    )

    # ---- where it listens --------------------------------------------------------------------------

    def preset(self):
        return PRESETS.get(self.values["freq"])

    def freqs(self) -> list[str]:
        f = self.values["freq"]
        if f in PRESETS:
            return list(PRESETS[f][2])
        return self.values["custom_freq"].split() if f == "custom" else [f]

    def wide_bands(self) -> list[tuple[float, float]] | None:
        """The bands to take whole: on a HackRF with "Whole bands at once"."""
        b = self.bridge()
        if not self.live() or b is None or b.device.get("type") != "hackrf" or not self.values.get("wide"):
            return None
        p = self.preset()
        if p is not None:
            return [BANDS[n] for n in p[1]]
        out = []
        for f in self.freqs():
            hz = float(f.rstrip("Mm")) * 1e6 if f[-1:] in "Mm" else float(f)
            out.append((hz - 0.5e6, hz + 0.5e6))
        return out

    def rtl433_args(self) -> list[str]:
        """What every rtl_433 gets: JSON and its log, time, level, noise and stats; protocols, your decoders, the
        analyser."""
        v = self.values
        # -F log: rtl_433's own messages (no device, errors), which -F json alone keeps quiet. noise:5 and
        # stats:1:10 report the noise floor and how many transmissions were seen.
        args = ["-F", "json", "-F", "log", "-M", "time:iso", "-M", "level", "-M", "noise:5", "-M", "stats:1:10"]
        for p in v["protocols"].split():
            args += ["-R", p]
        for spec in (s.strip() for s in v.get("flex", "").split(";")):
            if spec:
                args += ["-X", spec]
        if v.get("analyse"):
            args.append("-A")
        return args

    def command(self):
        v = self.values
        self._reset()
        wide = self.wide_bands()
        if wide is not None:
            b = self.bridge()
            self._wide = True
            return system.python_command(
                "sdr_toolkit.engines.ism_wide", "--port", str(b.local_port),
                "--bands", ",".join(f"{int(lo)}-{int(hi)}" for lo, hi in wide), "--hop", str(v["hop"]),
                "--ppm", str(v["ppm"]), "--", *self.rtl433_args())
        cmd = ["rtl_433", *self.rtl433_args(), "-s", v["rate"]]
        if self.live():
            b = self.bridge()
            cmd += ["-d", f"rtl_tcp:127.0.0.1:{b.local_port}" if b else self.device_index(), "-p", str(v["ppm"])]
            if self.gain_db():
                cmd += ["-g", self.gain_db()]
            for f in self.freqs():
                cmd += ["-f", f]
            if len(self.freqs()) > 1:
                cmd += ["-H", str(v["hop"])]
        else:
            cmd += ["-r", f"cu8:{self.file_path()}"]
        return cmd

    # ---- what it hears ----------------------------------------------------------------------------

    def _reset(self):
        self._decoded = self._seen = 0
        self._noise = None
        self._wide = False
        self._band = ""
        self._devices: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._last_decoded = 0.0
        self._analysis: dict[str, dict] = {}            # per rtl_433 (its channel tag): the burst being analysed
        self._pending: list[tuple[float, dict]] = []     # analysed bursts waiting to see if something decoded them
        self._added: set[str] = set()

    def parse(self, line):
        if not hasattr(self, "_devices"):
            self._reset()
        line = line.strip()
        tag = ""
        m = re.match(r"\[(\d+\.\d+)\] (.*)", line)
        if m:                                             # from one of ism_wide's rtl_433s: its channel
            tag, line = m.group(1), m.group(2)
        if not line.startswith("{"):
            return self._text(line, tag)
        try:
            d = json.loads(line)
        except ValueError:
            return []
        if d.get("event") == "band":
            self._band = f"{d['lo'] / 1e6:g}-{d['hi'] / 1e6:g} MHz whole ({len(d['channels'])} channels at once)"
            return [Event("status", f"listening to {self._band}")]
        if "model" not in d:
            if isinstance(d.get("frames"), dict):           # a stats report
                self._seen += int(d["frames"].get("count", 0))
            return []
        self._decoded += 1
        self._last_decoded = time.time()
        return self._device(d)

    def _text(self, line, tag):
        """rtl_433's other lines: its log, the noise floor, and the pulse analyser's reports."""
        problem = self.device_problem(line)
        if problem is not None:
            return problem
        if getattr(self, "_problem", None):
            return []
        m = re.search(r"estimated noise (-?[\d.]+) dB", line)
        if m:
            self._noise = float(m.group(1))
            return []
        a = self._analysis.get(tag)
        if line.startswith("Detected ") and "package" in line:      # "Detected OOK package @..."
            self._analysis[tag] = {"mod": line.split()[1], "chan": tag, "t": time.time()}
            return []
        if a is not None:
            if line.startswith("Guessing modulation:"):
                a["guess"] = line.split(":", 1)[1].strip()
                if "No clue" in a["guess"]:
                    self._pending.append((a["t"], self._analysis.pop(tag)))
            elif line.startswith("RSSI:"):
                m = re.search(r"RSSI: (-?[\d.]+) dB SNR: (-?[\d.]+)", line)
                if m:
                    a["rssi"], a["snr"] = float(m.group(1)), float(m.group(2))
            elif line.startswith("Frequency offsets"):
                m = re.search(r"\(([+-][\d.]+) kHz", line)
                if m:
                    a["offset_khz"] = float(m.group(1))
            elif line.startswith("Use a flex decoder with -X"):
                m = re.search(r"-X '([^']+)'", line)
                if m:
                    a["flex"] = m.group(1)
                self._pending.append((a["t"], self._analysis.pop(tag)))
            return []
        if looks_like_error(line):
            return [Event("status", line), Event("line", line, style="warn")]
        return []

    @staticmethod
    def _new_entry(kind, model, ident, ch, now):
        return {"kind": kind, "model": model, "id": ident, "channel": ch, "first": now, "count": 0,
                "times": collections.deque(maxlen=30), "freqs": collections.Counter(),
                "history": collections.defaultdict(lambda: collections.deque(maxlen=200))}

    def _device(self, d):
        now = time.time()
        model = str(d.get("model", ""))
        ident = d.get("id", "")
        ch = d.get("channel", "")
        key = model + (f" #{ident}" if ident != "" else "") + (f" ch{ch}" if ch != "" else "")
        readings = {k: v for k, v in d.items() if k not in META}
        new = key not in self._devices
        dev = self._devices.get(key) or self._devices.setdefault(key, self._new_entry("device", model, ident, ch, now))
        dev["count"] += 1
        dev["last"] = now
        dev["times"].append(now)
        dev["readings"] = readings
        dev["battery"] = d.get("battery_ok")
        dev["mod"] = d.get("mod", dev.get("mod", ""))
        if isinstance(d.get("freq"), (int, float)):
            dev["freq"] = float(d["freq"])
            dev["freqs"][round(float(d["freq"]), 1)] += 1
        if "rssi" in d:
            dev["rssi"], dev["snr"] = d.get("rssi"), d.get("snr")
        for k, v in readings.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                dev["history"][k].append((now, v))
        self._dirty = True
        text = f"{key}: " + ", ".join(f"{k}={v}" for k, v in readings.items())
        if "freq" in dev:
            text += f" ({dev['freq']:.3f} MHz)"
        events = [Event("log", text)]
        if new:
            where = f" on {dev['freq']:.3f} MHz" if "freq" in dev else ""
            events.append(Event("line", f"New device: {key}{where}", style="dim"))
        return events

    def _unknown(self, a):
        """An analysed burst nothing decoded: grouped with others of the same modulation and timings."""
        now = time.time()
        spec = a.get("flex", "")
        fields = dict(p.split("=", 1) for p in spec.split(",") if "=" in p)
        mod = fields.get("m", a.get("mod", "?"))
        timing = "/".join(f"{int(round(int(fields[k]) / 20.0) * 20)}" for k in ("s", "l")
                          if fields.get(k, "").isdigit())
        key = f"Unknown {mod}" + (f" {timing} µs" if timing else "")
        dev = self._devices.get(key) or self._devices.setdefault(key, self._new_entry("unknown", "Unknown signal",
                                                                                       "", "", now))
        dev["count"] += 1
        dev["last"] = now
        dev["times"].append(now)
        dev["flex"] = spec
        dev["guess"] = a.get("guess", "")
        dev["readings"] = {"modulation": a.get("guess", mod)}
        if a.get("chan"):
            f = float(a["chan"]) + a.get("offset_khz", 0) / 1000
            dev["freq"] = f
            dev["freqs"][round(f, 1)] += 1
        if "rssi" in a:
            dev["rssi"], dev["snr"] = a["rssi"], a["snr"]
        self._dirty = True
        return [Event("line", f"New unknown signal: {key}", style="dim")] if dev["count"] == UNKNOWN_SEEN else []

    def _settle(self, final=False) -> list:
        """Analysed bursts old enough to judge: unknown unless a decoded message came close to them."""
        now, events, keep = time.time(), [], []
        for t, a in getattr(self, "_pending", []):
            if abs(self._last_decoded - t) < UNKNOWN_QUIET:
                continue                                       # a decoder named it
            if now - t < UNKNOWN_QUIET and not final:
                keep.append((t, a))
            else:
                events += self._unknown(a)
        self._pending = keep
        return events

    def poll(self):
        events = super().poll()
        events += self._settle()
        now = time.time()
        if getattr(self, "_dirty", False) and now - self._last_table >= 1.0:
            self._last_table = now
            self._dirty = False
            events.append(Event("table", data={"rows": self.rows()}))
        return events

    @staticmethod
    def _every(times) -> str:
        t = list(times)
        if len(t) < 3:
            return ""
        gaps = [b - a for a, b in zip(t, t[1:]) if b - a > 0.5]   # repeats within one transmission don't count
        return f"{statistics.median(gaps):.0f} s" if gaps else ""

    def rows(self) -> list[dict]:
        out = []
        for key, dev in sorted(self._devices.items(), key=lambda kv: -kv[1]["last"]):
            if dev["kind"] == "unknown" and dev["count"] < UNKNOWN_SEEN:
                continue
            readings = ", ".join(f"{k}={v}" for k, v in dev.get("readings", {}).items())
            if dev.get("battery") is not None:
                readings += ("" if not readings else ", ") + ("battery OK" if dev["battery"] else "battery LOW")
            signal = ""
            if self.values.get("level") and dev.get("rssi") is not None:
                signal = f"{dev['rssi']:.0f} dB, SNR {dev.get('snr') or 0:.0f}"
            out.append({"key": key, "readings": readings, "freq": f"{dev['freq']:.3f}" if "freq" in dev else "",
                        "count": dev["count"], "every": self._every(dev["times"]), "signal": signal,
                        "last": time.strftime("%H:%M:%S", time.localtime(dev["last"])),
                        "first": time.strftime("%H:%M:%S", time.localtime(dev["first"]))})
        return out

    def details(self, key):
        dev = getattr(self, "_devices", {}).get(key)
        if dev is None:
            return None
        f = [("Heard", f"{dev['count']} times"),
             ("First heard", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(dev["first"]))),
             ("Last heard", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(dev["last"])))]
        every = self._every(dev["times"])
        if every:
            f.append(("Sends every", every))
        if dev["freqs"]:
            f.append(("Frequency", ", ".join(f"{fr:.1f} MHz ({n}×)" for fr, n in dev["freqs"].most_common(4))))
        if dev.get("rssi") is not None:
            f.append(("Signal", f"{dev['rssi']:.1f} dB, SNR {dev.get('snr') or 0:.1f} dB"))
        if dev["kind"] == "unknown":
            f += [("Modulation", dev.get("guess", "")), ("Decoder spec", dev.get("flex") or "(none: no clue)")]
            return {"title": key, "subtitle": "Not decoded: its pulses, as rtl_433's analyser measured them",
                    "fields": f}
        if dev.get("mod"):
            f.append(("Modulation", dev["mod"]))
        if dev.get("battery") is not None:
            f.append(("Battery", "OK" if dev["battery"] else "LOW"))
        for k, v in dev.get("readings", {}).items():
            h = [x for _, x in dev["history"].get(k, ())]
            span = (f" (range {min(h):g} to {max(h):g} over {len(h)} readings)"
                    if len(h) > 1 and min(h) != max(h) else "")
            f.append((k, f"{v}{span}"))
        sub = " · ".join(str(x) for x in (dev["model"], f"ID {dev['id']}" if dev["id"] != "" else "",
                                           f"channel {dev['channel']}" if dev["channel"] != "" else "") if x)
        return {"title": key, "subtitle": sub, "fields": f}

    # ---- an unknown signal as a decoder of your own ----------------------------------------------------

    def _newest_unknown(self):
        cands = [(dev["last"], key, dev) for key, dev in getattr(self, "_devices", {}).items()
                 if dev["kind"] == "unknown" and dev["count"] >= UNKNOWN_SEEN and dev.get("flex")
                 and key not in self._added and dev["flex"] not in self.values.get("flex", "")]
        return max(cands)[1:] if cands else None

    def actions(self):
        u = self._newest_unknown()
        if u is None:
            return []
        key, dev = u
        return [("flex", f"Add “{key}” as a decoder", True,
                 f"Adds rtl_433's suggested decoder ({dev['flex']}) to Your decoders: its messages then come out "
                 "decoded, with their bits.")]

    def run_action(self, action_id):
        u = self._newest_unknown()
        if action_id != "flex" or u is None:
            return {}
        key, dev = u
        self._added.add(key)
        n = len([s for s in self.values.get("flex", "").split(";") if s.strip()]) + 1
        spec = re.sub(r"\bn=[^,]*", f"n=signal_{n}", dev["flex"])
        old = self.values.get("flex", "").strip()
        return {"flex": f"{old}; {spec}" if old else spec}

    def listening_on(self):
        if getattr(self, "_wide", False):
            return self._band or "whole bands"
        p = self.preset()
        if p is not None:
            return p[0].split(":", 1)[0] + " (hopping)"
        return " + ".join(f.rstrip("M") for f in self.freqs()) + " MHz"

    def activity(self) -> str:
        devices = getattr(self, "_devices", {})
        unknown = sum(1 for d in devices.values() if d["kind"] == "unknown" and d["count"] >= UNKNOWN_SEEN)
        devices = {k: d for k, d in devices.items() if d["kind"] != "unknown" or d["count"] >= UNKNOWN_SEEN}
        parts = [super().activity(), f"{len(devices) - unknown} devices"
                 + (f", {unknown} unknown signals" if unknown else "")]
        if getattr(self, "_noise", None) is not None:
            parts.append(f"noise {self._noise:.0f} dB")
        seen, decoded = getattr(self, "_seen", 0), getattr(self, "_decoded", 0)
        parts.append(f"{seen} transmission{'s' if seen != 1 else ''} seen, {decoded} decoded")
        return " · ".join(parts)

    def stop(self):
        events = super().stop()
        events += self._settle(final=True)
        if getattr(self, "_devices", None):                  # the last second's changes
            events.append(Event("table", data={"rows": self.rows()}))
        return events

    def finished(self, code):
        if not self.live():
            return [Event("status", "recording finished")]
        return super().finished(code)
