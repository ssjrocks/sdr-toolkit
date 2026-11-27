"""Utility meters: ERT meters (Itron and others: gas, water, electricity on 902-928 MHz) through rtlamr, and
Wireless M-Bus meters and sensors (water, heat, gas, electricity, room sensors on 868 MHz) through rtl_wmbus
and wmbusmeters, which picks a driver for each meter by itself. One row per meter, kept up to date.

rtlamr talks rtl_tcp: to the relay bridge, or to an rtl_tcp started here for this PC's dongle."""

import collections
import json
import logging
import statistics
import time

from .. import system
from .base import Event, Setting, Tuning
from .process import looks_like_error
from .radio import RADIO_SETTINGS, RadioMode

# rtlamr's SCM / IDM endpoint types, as its documentation lists them.
ERT_COMMODITY = {**{t: "electricity" for t in (4, 5, 7, 8)}, **{t: "gas" for t in (2, 9, 12)},
                 **{t: "water" for t in (11, 13)}}
WMBUS = {"wmbus": (868.95e6, "t1,c1"), "wmbus_s": (868.3e6, "s1")}
log = logging.getLogger(__name__)


def rtlamr() -> str | None:
    p = system.data_dir() / "rtlamr" / "bin" / "rtlamr"
    return str(p) if p.is_file() else None


class MetersMode(RadioMode):
    id = "meters"
    name = "Utility meters (ERT, Wireless M-Bus)"
    group = "SDR"
    summary = ("Gas, water and electricity meters that report by radio: ERT meters (Itron and others) on "
               "902-928 MHz, and Wireless M-Bus meters and sensors on 868 MHz. One row per meter, with its "
               "reading. Decoded by rtlamr and wmbusmeters.")
    output = "table"
    columns = (("key", "Meter"), ("reading", "Reading"), ("kind", "What"), ("count", "Heard"),
               ("every", "Sends every"), ("signal", "Signal"), ("last", "Last heard"), ("first", "First heard"))
    key_column = "key"
    requires = ("rtlamr", "wmbusmeters")
    settings = RADIO_SETTINGS + (
        Setting("kind", "Meters", "ert", "choice", restart=True,
                choices=(("ert", "ERT: Itron and others, 902-928 MHz (gas, water, electricity)"),
                         ("wmbus", "Wireless M-Bus T1 / C1, 868.95 MHz (water, heat, gas, electricity, sensors)"),
                         ("wmbus_s", "Wireless M-Bus S1, 868.3 MHz")),
                help="ERT is used in North America and parts of Australia and NZ; Wireless M-Bus mostly in Europe."),
        Setting("msgtype", "ERT messages", "all", "choice", restart=True,
                choices=(("all", "All: SCM, SCM+, IDM, NetIDM, R900"), ("scm", "SCM (most meters)"),
                         ("scm+", "SCM+"), ("idm", "IDM (interval data, electricity)"), ("netidm", "NetIDM"),
                         ("r900", "R900 (Neptune water meters)"), ("r900bcd", "R900 BCD")),
                help="Which message types rtlamr listens for. All costs more CPU."),
        Setting("centre_mhz", "ERT centre frequency", 912.6, "float", 900.0, 930.0, 0.1, 3, suffix=" MHz",
                restart=True, help="rtlamr's 912.6 MHz covers the channels ERT meters hop over (910-920 MHz)."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the SDR (the toolkit tunes it)",
        bandwidth="2.4 MHz for ERT, 1.6 MHz for Wireless M-Bus",
        frequencies=(("910-920 MHz", "ERT meters: SCM, IDM, R900 (rtlamr listens at 912.6 MHz)"),
                     ("868.95 MHz", "Wireless M-Bus T1 and C1 (Europe)"),
                     ("868.3 MHz", "Wireless M-Bus S1"),
                     ("169 MHz", "Wireless M-Bus N mode (not yet decoded here)")),
        notes="Meters report every 30 s to a few minutes. Encrypted Wireless M-Bus meters are listed by ID, type "
              "and maker without their readings: those need the meter's key.",
    )

    # ---- what runs ---------------------------------------------------------------------------------

    def upstream(self):
        v = self.values
        if not self.live():
            return None
        if v["kind"] == "ert":
            return None                                       # rtlamr talks rtl_tcp: the bridge's or _rtl_tcp's
        freq, _ = WMBUS[v["kind"]]
        if self.bridge():
            return self.relay_stream(freq, 1_600_000)
        cmd = ["rtl_sdr", "-d", self.device_index().lstrip(":"), "-f", str(int(freq)), "-s", "1600000",
               "-p", str(v["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def command(self):
        v = self.values
        self._reset()
        if v["kind"] == "ert":
            exe = rtlamr()
            if exe is None:
                from .base import MissingParts
                raise MissingParts(["rtlamr"])
            if not self.live():
                raise RuntimeError("ERT meters need the SDR live: rtlamr can't read a recording.")
            b = self.bridge()
            self._port = b.local_port if b else self._free_port()
            if not b:
                self._start_rtl_tcp()
            amr = [exe, f"-server=127.0.0.1:{self._port}", f"-msgtype={v['msgtype']}", "-format=json",
                   f"-centerfreq={int(float(v['centre_mhz']) * 1e6)}"]
            if self.gain_db():
                amr.append(f"-tunergain={self.gain_db()}")
            if b:
                return amr
            # Wait for this PC's rtl_tcp to listen (rtlamr gives up at once otherwise), asking the system rather
            # than connecting: rtl_tcp starts streaming to a test connection and exits on its broken pipe.
            wait = (f"for i in $(seq 100); do ss -Hltn 'sport = :{self._port}' | grep -q . && break; "
                    "sleep 0.1; done; ")
            return ["bash", "-c", wait + "exec " + " ".join(f"'{a}'" for a in amr)]
        _, modes = WMBUS[v["kind"]]
        meters = "wmbusmeters --silent --format=json stdin:rtlwmbus AnyMeter auto '*' NOKEY"
        if not self.live():
            return ["bash", "-c", f"rtl_wmbus < '{self.file_path()}' | {meters}"]
        return ["bash", "-c", f"rtl_wmbus | {meters}"]

    # ---- what it hears ----------------------------------------------------------------------------

    def _reset(self):
        self._meters: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._decoded = 0

    def parse(self, line):
        if not hasattr(self, "_meters"):
            self._reset()
        line = line.strip()
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            if looks_like_error(line) and "dll_len adjusted" not in line:
                return [Event("status", line), Event("line", line, style="warn")]
            return []
        try:
            d = json.loads(line)
        except ValueError:
            return []
        if "Message" in d and "Type" in d:                  # rtlamr
            return self._ert(d)
        if d.get("_") == "telegram" or "media" in d:        # wmbusmeters
            return self._wmbus(d)
        return []

    def _ert(self, d):
        m = d["Message"]
        typ = d["Type"]
        ident = m.get("ID", m.get("EndpointID", m.get("ERTSerialNumber", "?")))
        ert_type = m.get("Type", m.get("EndpointType", m.get("ERTType")))
        reading = m.get("Consumption", m.get("LastConsumptionCount", m.get("LastConsumption")))
        what = f"{typ}" + (f" · {ERT_COMMODITY.get(int(ert_type), f'type {ert_type}')}" if ert_type is not None else "")
        fields = {k: v for k, v in m.items() if k not in ("ChecksumVal", "PacketCRC", "Preamble")}
        return self._seen(f"{typ} {ident}", reading, what, fields)

    def _wmbus(self, d):
        ident = d.get("id", "?")
        meter = d.get("meter", "")
        values = {k: v for k, v in d.items() if k not in ("_", "name", "id", "meter", "media", "timestamp", "device",
                                                          "rssi_dbm")}
        main = next((f"{v} {k.rsplit('_', 1)[-1]}" for k, v in values.items()
                     if k.startswith(("total_", "current_", "consumption")) and isinstance(v, (int, float))), "")
        what = " · ".join(x for x in (d.get("media", ""), meter) if x)
        return self._seen(f"M-Bus {ident}", main, what, values, rssi=d.get("rssi_dbm"))

    def _seen(self, key, reading, what, fields, rssi=None):
        now = time.time()
        new = key not in self._meters
        m = self._meters.setdefault(key, {"first": now, "count": 0, "times": collections.deque(maxlen=30)})
        m.update(last=now, reading=reading, what=what, fields=fields)
        m["count"] += 1
        m["times"].append(now)
        if rssi is not None:
            m["rssi"] = rssi
        self._decoded += 1
        self._dirty = True
        events = [Event("log", f"{key} ({what}): {reading}")]
        if new:
            events.append(Event("line", f"New meter: {key} ({what})", style="dim"))
        return events

    def poll(self):
        events = super().poll() + self.poll_rtl_tcp()
        now = time.time()
        if getattr(self, "_dirty", False) and now - self._last_table >= 1.0:
            self._last_table = now
            self._dirty = False
            events.append(Event("table", data={"rows": self.rows()}))
        return events

    def poll_rtl_tcp(self) -> list:
        line, self._rtl_tcp_problem = getattr(self, "_rtl_tcp_problem", None), None
        return (self.device_problem(line) or []) if line else []

    def stop(self):
        events = super().stop()                              # rtlamr first, then its rtl_tcp
        self._stop_rtl_tcp()
        if getattr(self, "_meters", None):
            events.append(Event("table", data={"rows": self.rows()}))
        return events

    def rows(self):
        out = []
        for key, m in sorted(self._meters.items(), key=lambda kv: -kv[1]["last"]):
            t = list(m["times"])
            gaps = [b - a for a, b in zip(t, t[1:]) if b - a > 0.5]
            out.append({"key": key, "reading": "" if m["reading"] is None else m["reading"], "kind": m["what"],
                        "count": m["count"], "every": f"{statistics.median(gaps):.0f} s" if len(gaps) >= 2 else "",
                        "signal": f"{m['rssi']} dBm" if m.get("rssi") is not None else "",
                        "last": time.strftime("%H:%M:%S", time.localtime(m["last"])),
                        "first": time.strftime("%H:%M:%S", time.localtime(m["first"]))})
        return out

    def details(self, key):
        m = getattr(self, "_meters", {}).get(key)
        if m is None:
            return None
        f = [("What", m["what"]), ("Heard", f"{m['count']} times"),
             ("First heard", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["first"]))),
             ("Last heard", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["last"])))]
        f += [(k, str(v)) for k, v in m["fields"].items()]
        return {"title": key, "subtitle": m["what"], "fields": f}

    def listening_on(self):
        v = self.values
        if v["kind"] == "ert":
            return f"{float(v['centre_mhz']):.1f} MHz (ERT, {v['msgtype']})"
        return f"{WMBUS[v['kind']][0] / 1e6:.2f} MHz (Wireless M-Bus)"

    def activity(self):
        n = len(getattr(self, "_meters", {}))
        return super().activity() + f" · {n} meter{'s' if n != 1 else ''}, {getattr(self, '_decoded', 0)} messages"
