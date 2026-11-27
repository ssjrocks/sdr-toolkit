"""Zigbee, Thread and other IEEE 802.15.4 devices at 2.4 GHz (light bulbs, sensors, switches, smart plugs, Matter
over Thread), heard on a HackRF: each network (PAN), each device by its address and maker, what kind of frames
it sends, how often and how strongly. The toolkit's own decoder (engines/zigbee.py)."""

import json
import time

from .. import system
from ..oui import vendor
from .base import Event, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode

RATE = 4_000_000


class ZigbeeMode(RadioMode):
    id = "zigbee"
    name = "Zigbee and Thread devices (2.4 GHz)"
    group = "Wi-Fi & Bluetooth"
    summary = ("Smart-home devices on IEEE 802.15.4 (Zigbee, Thread, Matter over Thread): each network and "
               "device with its address and maker, the frames it sends, how often and how strongly. Needs a "
               "HackRF (2.4 GHz).")
    output = "table"
    requires = ("rtl-sdr",)
    key_column = "device"
    columns = (("device", "Device"), ("maker", "Maker"), ("pan", "Network (PAN)"), ("protocol", "Protocol"),
               ("kinds", "Frames"), ("count", "Heard"), ("rssi", "dBFS"), ("channel", "Ch"), ("last", "Last heard"))
    settings = RADIO_SETTINGS + (
        Setting("channels", "Channels", "all", "choice", restart=True,
                choices=(("all", "All 16 (11-26), hopping"), ("zigbee", "Zigbee's usual (11, 15, 20, 25)"),
                         *((str(c), f"Channel {c} ({2405 + 5 * (c - 11)} MHz)") for c in range(11, 27)))),
        Setting("dwell", "Time on each channel", 3.0, "float", 0.5, 60.0, 0.5, 1, suffix=" s", restart=True),
        Setting("file_rate", "Recording sample rate", RATE, "int", 2000000, 20000000, 2000000, suffix=" Hz",
                restart=True, help="A multiple of 2 MS/s, centred on one channel."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ at 4 MS/s from a HackRF, one 802.15.4 channel at a time",
        bandwidth="Each channel 2 MHz wide, 5 MHz apart from 2405 to 2480 MHz",
        frequencies=(("2405-2480 MHz", "802.15.4 channels 11-26: Zigbee, Thread, Matter"),),
        notes="A Zigbee network stays on one channel (often 11, 15, 20 or 25): once you've seen it, pick that "
              "channel. Most traffic is encrypted at the network layer: who talks to whom, how often and how "
              "strongly still shows. An RTL-SDR can't reach 2.4 GHz.",
    )

    def listening_on(self):
        c = self.values["channels"]
        return "channels 11-26" if c == "all" else "channels 11, 15, 20, 25" if c == "zigbee" else f"channel {c}"

    def _channels(self) -> str:
        c = self.values["channels"]
        if c == "all":
            return ",".join(str(x) for x in range(11, 27))
        if c == "zigbee":
            return "11,15,20,25"
        return c

    def command(self):
        self._devices: dict[str, dict] = {}
        self._pans: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._channel = None
        cmd = system.python_command("sdr_toolkit.engines.zigbee")
        if not self.live():
            return cmd + ["--rate", str(int(self.values["file_rate"])), "--input", self.file_path()]
        b = self.bridge()
        if b is None or b.device.get("type") != "hackrf":
            raise RuntimeError("Zigbee and Thread are at 2.4 GHz, out of an RTL-SDR's reach: this needs a HackRF "
                               "(plugged in here, or on a relay).")
        return cmd + ["--rate", str(RATE), "--port", str(b.local_port), "--channels", self._channels(),
                      "--dwell", str(self.values["dwell"])]

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            return [Event("line", line.strip(), style="warn")] if "zigbee:" in line else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        if e.get("event") == "channel":
            self._channel = e["channel"]
            return [Event("status", self.activity())]
        if e.get("event") != "frame":
            return []
        events = []
        pan = e.get("pan", "")
        if e.get("type") == "beacon" and pan:
            new = pan not in self._pans
            p = self._pans.setdefault(pan, {})
            p.update(channel=e.get("channel"), ext_pan=e.get("ext_pan", p.get("ext_pan", "")),
                     permit_join=e.get("permit_join"), protocol=e.get("protocol") or p.get("protocol", ""))
            if new:
                events.append(Event("line", f"Network {pan} on channel {e.get('channel')}"
                                            + (f" ({p['protocol']}, extended PAN ID {p['ext_pan']})" if p.get("ext_pan") else "")
                                            + (" · open for joining" if e.get("permit_join") else "")))
        dev = e.get("src_ext") or e.get("src")
        if not dev:
            if e.get("type") == "ack":
                return events
            dev = "(no source address)"
        key = f"{dev}@{pan}" if e.get("src") and not e.get("src_ext") else dev
        d = self._devices.get(key)
        if d is None:
            d = self._devices[key] = {"first": time.time(), "count": 0, "kinds": {}, "dst": set()}
            maker = vendor(dev) if e.get("src_ext") else ""
            events.append(Event("log", f"New device {dev}" + (f" ({maker})" if maker else "") + f" on PAN {pan or '?'}"
                                       + (f", channel {e.get('channel')}" if e.get("channel") else "")))
        d["count"] += 1
        d["kinds"][e.get("type", "?")] = d["kinds"].get(e.get("type", "?"), 0) + 1
        d.update(device=dev, pan=pan, rssi=e.get("rssi"), channel=e.get("channel"), last=time.time(),
                 protocol=e.get("protocol") or d.get("protocol", ""), security=e.get("security"),
                 maker=vendor(dev) if e.get("src_ext") else d.get("maker", ""))
        if e.get("dst") or e.get("dst_ext"):
            d["dst"].add(e.get("dst_ext") or e.get("dst"))
        self._dirty = True
        return events

    def poll(self):
        events = super().poll()
        if getattr(self, "_dirty", False) and time.time() - self._last_table >= 1.0:
            self._dirty = False
            self._last_table = time.time()
            events.append(self._table())
        return events

    def stop(self):
        events = super().stop()
        if getattr(self, "_devices", None):
            events.append(self._table())
        return events

    def _table(self):
        rows = []
        for key, d in sorted(self._devices.items(), key=lambda kv: -kv[1]["count"]):
            rows.append({"device": key, "maker": d.get("maker", ""), "pan": d.get("pan", ""),
                         "protocol": d.get("protocol", ""),
                         "kinds": ", ".join(f"{k} ×{v}" for k, v in sorted(d["kinds"].items())), "count": d["count"],
                         "rssi": d.get("rssi"), "channel": d.get("channel"),
                         "last": time.strftime("%H:%M:%S", time.localtime(d["last"]))})
        return Event("table", data={"rows": rows})

    def details(self, key):
        d = getattr(self, "_devices", {}).get(key)
        if d is None:
            return None
        p = self._pans.get(d.get("pan", ""), {})
        f = [("Address", d["device"]), ("Maker", d.get("maker", "")), ("Network (PAN ID)", d.get("pan", "")),
             ("Extended PAN ID", p.get("ext_pan", "")), ("Protocol", d.get("protocol", "")),
             ("Channel", str(d.get("channel") or "")), ("Frames", ", ".join(f"{k} ×{v}" for k, v in d["kinds"].items())),
             ("Encrypted (MAC layer)", "yes" if d.get("security") else "no"),
             ("Sends to", ", ".join(sorted(d["dst"]))[:200]), ("Signal", f"{d.get('rssi')} dBFS")]
        return {"title": d["device"], "subtitle": d.get("maker") or d.get("protocol", ""),
                "fields": [x for x in f if x[1]]}

    def activity(self):
        ch = getattr(self, "_channel", None)
        return (super().activity() + f" · {len(getattr(self, '_devices', {}))} devices, "
                f"{len(getattr(self, '_pans', {}))} networks" + (f" · on channel {ch}" if ch else ""))
