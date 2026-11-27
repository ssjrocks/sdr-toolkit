"""Wi-Fi in monitor mode: every network around (name, channel, signal, security), the devices on each, the
networks devices are looking for, and how busy each channel is; with your own network's password, who your
devices talk to. Uses this PC's Wi-Fi card: a monitor interface of its own (set up through a password prompt),
your normal Wi-Fi left as it is (engines/wifi.py, setup/wifi_monitor.sh)."""

import json
import shutil
import subprocess
import time

from .. import deps, system
from .base import Event, MissingParts, Setting, Tuning
from .process import ProcessMode

BANDS = {
    "24": "1 2 3 4 5 6 7 8 9 10 11 12 13",
    "5": "36 40 44 48 52 56 60 64 100 104 108 112 116 120 124 128 132 136 140 144 149 153 157 161 165",
}


def wifi_phy() -> str | None:
    """The first Wi-Fi card that can do monitor mode."""
    try:
        out = subprocess.run(["iw", "list"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    phy, ok = None, {}
    for line in out.splitlines():
        if line.startswith("Wiphy "):
            phy = line.split()[1]
        elif phy and line.strip() == "* monitor":
            ok[phy] = True
    return next(iter(ok), None)


def wifi_off() -> bool:
    try:
        out = subprocess.run(["rfkill", "list", "wifi"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "Soft blocked: yes" in out or "Hard blocked: yes" in out


class WiFiMode(ProcessMode):
    id = "wifi"
    name = "Wi-Fi networks and devices"
    group = "Wi-Fi & Bluetooth"
    source = "none"
    summary = ("Every Wi-Fi network around and the devices on each, the networks devices are looking for, and "
               "channel use; with your own network's password, who your devices talk to. Uses this PC's Wi-Fi card "
               "in monitor mode (it asks for your password to set that up).")
    output = "table"
    key_column = "bssid"
    columns = (("ssid", "Network"), ("bssid", "BSSID"), ("channel", "Ch"), ("security", "Security"),
               ("signal", "dBm"), ("devices", "Devices"), ("last", "Last heard"))
    settings = (
        Setting("input", "Input", "live", "choice", restart=True,
                choices=(("live", "This PC's Wi-Fi card"), ("file", "A capture file (pcap)"))),
        Setting("file", "Capture file", "", "text", restart=True),
        Setting("band", "Channels", "both", "choice", restart=True,
                choices=(("both", "2.4 and 5 GHz"), ("24", "2.4 GHz (1-13)"), ("5", "5 GHz"))),
        Setting("dwell", "Time on each channel", 0.5, "float", 0.1, 10.0, 0.1, 1, suffix=" s", restart=True),
        Setting("key", "Your network (to decrypt it)", "", "text", restart=True,
                help="PASSWORD:NETWORK NAME of your own WPA2 network: its traffic is decrypted for the devices whose "
                     "connection (the 4-way handshake) is heard after starting, showing who they talk to. Kept "
                     "in this PC's settings."),
    )
    tuning = Tuning(
        sdr_mode="No SDR: this PC's Wi-Fi card, in monitor mode on an interface of its own, hopping channels",
        bandwidth="20 MHz Wi-Fi channels; a hop every half second covers 2.4 GHz in 7 s, both bands in 20 s",
        frequencies=(("2.412-2.472 GHz", "Wi-Fi channels 1-13"), ("5.18-5.825 GHz", "Wi-Fi channels 36-165")),
        notes="Devices are seen when they send: phones look for their known networks (probe requests) every few "
              "minutes. Only your own network's traffic is decrypted, with its password, and only for devices "
              "that (re)connect while it runs. Open in Wireshark shows the raw frames.",
    )

    def live(self) -> bool:
        return self.values["input"] == "live"

    def channels(self) -> str:
        b = self.values["band"]
        return f"{BANDS['24']} {BANDS['5']}" if b == "both" else BANDS[b]

    def upstream(self):
        if not self.live():
            return None
        if wifi_off():
            raise RuntimeError("Wi-Fi is switched off on this PC (rfkill): turn it on in Settings (or "
                               "`rfkill unblock wifi`) to use this page. Your normal connection isn't changed by it.")
        phy = wifi_phy()
        if phy is None:
            raise RuntimeError("No Wi-Fi card here can do monitor mode.")
        self._phy = phy
        return ["pkexec", "bash", deps.setup_script("wifi_monitor.sh"), phy, self.channels(), str(self.values["dwell"])]

    def command(self):
        if shutil.which("tshark") is None:
            raise MissingParts(["tshark"])
        self._aps: dict[str, dict] = {}
        self._clients: dict[str, dict] = {}
        self._probes: dict[str, set] = {}
        self._traffic: dict[str, dict] = {}
        self._chan: dict[int, int] = {}
        self._dirty = False
        self._last_table = 0.0
        cmd = system.python_command("sdr_toolkit.engines.wifi")
        if self.live():
            cmd += ["--iface", ""]
        else:
            if not self.values["file"]:
                raise RuntimeError("Choose a capture file (pcap) in Settings, or set Input to this PC's Wi-Fi card.")
            cmd += ["--pcap", str(self.values["file"])]
        if self.values["key"].strip():
            cmd += ["--key", self.values["key"].strip()]
        return cmd

    def parse(self, line):
        if not line.startswith("{"):
            if "wifi:" in line:
                return [Event("line", line.strip(), style="warn"), Event("status", line.strip())]
            return []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        k = e.get("event")
        events = []
        if k == "ap":
            new = e["bssid"] not in self._aps
            a = self._aps.setdefault(e["bssid"], {"first": e["t"]})
            a.update(e)
            if new:
                events.append(Event("log", f"Network {e['ssid'] or '(hidden)'} ({e['bssid']}) on channel {e['channel']}, "
                                           f"{e['security']}, {e['signal']} dBm"))
        elif k == "client":
            c = self._clients.setdefault(e["mac"], {"first": e["t"]})
            if c.get("bssid") != e["bssid"]:
                ap = self._aps.get(e["bssid"], {})
                events.append(Event("log", f"Device {e['mac']} on {ap.get('ssid') or e['bssid']}"))
            c.update(e)
        elif k == "probe":
            seen = self._probes.setdefault(e["mac"], set())
            if e["ssid"] and e["ssid"] not in seen:
                seen.add(e["ssid"])
                events.append(Event("log", f"Device {e['mac']} is looking for the network \"{e['ssid']}\""))
        elif k == "traffic":
            tr = self._traffic.setdefault(e["mac"], {})
            dest = e.get("name") or e.get("to")
            if dest and dest not in tr:
                tr[dest] = e["t"]
                events.append(Event("log", f"{e['mac']} → {dest}"))
        elif k == "channel" and e.get("channel"):
            self._chan[e["channel"]] = e["frames"]
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
        if getattr(self, "_aps", None):
            events.append(self._table())
        return events

    def _table(self):
        counts: dict[str, int] = {}
        for c in self._clients.values():
            counts[c["bssid"]] = counts.get(c["bssid"], 0) + 1
        rows = []
        for b, a in sorted(self._aps.items(), key=lambda kv: -(kv[1].get("signal") or -999)):
            rows.append({"ssid": a.get("ssid") or "(hidden)", "bssid": b, "channel": a.get("channel"),
                         "security": a.get("security", ""), "signal": a.get("signal"), "devices": counts.get(b, 0),
                         "last": time.strftime("%H:%M:%S", time.localtime(a["t"]))})
        return Event("table", data={"rows": rows})

    def details(self, key):
        a = getattr(self, "_aps", {}).get(key)
        if a is None:
            return None
        f = [("Network", a.get("ssid") or "(hidden)"), ("BSSID", key), ("Channel", str(a.get("channel"))),
             ("Security", a.get("security", "")), ("Signal", f"{a.get('signal')} dBm")]
        for mac, c in sorted(self._clients.items()):
            if c["bssid"] != key:
                continue
            looking = ", ".join(sorted(self._probes.get(mac, ())))
            talks = ", ".join(list(self._traffic.get(mac, {}))[:6])
            f.append((f"Device {mac}", " · ".join(x for x in (f"{c['signal']} dBm" if c.get("signal") else "",
                                                               f"looks for {looking}" if looking else "",
                                                               f"talks to {talks}" if talks else "") if x) or "seen"))
        return {"title": a.get("ssid") or key, "subtitle": a.get("security", ""), "fields": f}

    def actions(self):
        if self.live() and shutil.which("wireshark"):
            return [("wireshark", "Open in Wireshark", True, "Wireshark on the monitor interface (sdrtkmon0)")]
        return []

    def run_action(self, action_id):
        if action_id == "wireshark":
            subprocess.Popen(["wireshark", "-k", "-i", "sdrtkmon0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {}

    def listening_on(self):
        return "this PC's Wi-Fi card" if self.live() else "a capture file"

    def activity(self):
        busiest = sorted(getattr(self, "_chan", {}).items(), key=lambda kv: -kv[1])[:3]
        return (super().activity() + f" · {len(getattr(self, '_aps', {}))} networks, "
                f"{len(getattr(self, '_clients', {}))} devices, {len(getattr(self, '_probes', {}))} looking"
                + (" · busiest: " + ", ".join(f"ch {c} ({n})" for c, n in busiest) if busiest else ""))
