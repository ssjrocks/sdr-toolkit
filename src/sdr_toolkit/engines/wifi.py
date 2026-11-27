"""Wi-Fi as heard in monitor mode: every network (its name, channel, signal, security), the devices on each,
the networks devices are looking for (probe requests), how busy each channel is, and on your own network (its
password given) who your devices talk to.

    python -m sdr_toolkit.engines.wifi --iface MON [--key "PASSWORD:SSID"]   (channel hops read from stdin)
    python -m sdr_toolkit.engines.wifi --pcap FILE [--key ...]

tshark reads the frames (live from the monitor interface the root helper set up, or a capture file) and gives
their fields; the helper's "CHANNEL n" lines arrive on stdin. JSON lines out:
  {"event": "ap", "bssid", "ssid", "channel", "signal", "security", "t"}
  {"event": "client", "mac", "bssid", "signal", "t"}          a device seen with a network
  {"event": "probe", "mac", "ssid", "signal", "t"}            a device looking for a network
  {"event": "traffic", "mac", "to", "name", "t"}              decrypted (your network): who a device talks to
  {"event": "channel", "channel", "frames"}                   at each hop: what the last channel carried
"""

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time

FIELDS = ["frame.time_epoch", "wlan.fc.type_subtype", "wlan.fc.ds", "wlan.bssid", "wlan.sa", "wlan.da", "wlan.ta",
          "wlan.ra", "radiotap.dbm_antsignal", "wlan_radio.channel", "wlan.ds.current_channel", "wlan.ssid",
          "wlan.fixed.capabilities.privacy", "wlan.rsn.akms.type", "ip.src", "ip.dst", "dns.qry.name",
          "tls.handshake.extensions_server_name", "http.host"]
AKM = {1: "WPA2-Enterprise", 2: "WPA2", 3: "WPA2-Enterprise (FT)", 4: "WPA2 (FT)", 5: "WPA2-Enterprise",
       6: "WPA2", 8: "WPA3", 9: "WPA3 (FT)", 12: "WPA3-Enterprise", 18: "OWE (encrypted open)", 24: "WPA3"}
BROADCAST = {"ff:ff:ff:ff:ff:ff", ""}
_lock = threading.Lock()


def emit(d):
    with _lock:
        sys.stdout.write(json.dumps(d) + "\n")
        sys.stdout.flush()


def _int(s, base=10):
    try:
        return int(str(s).split(",")[0], 0 if base == 0 else base)
    except ValueError:
        return None


def ssid_text(s: str) -> str:
    """tshark 4.6 gives an SSID as hex digits, older versions as text: hex is decoded when it makes UTF-8."""
    s = s.split(",")[0].replace(":", "")
    if s == "<MISSING>":
        return ""
    if s and len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            return bytes.fromhex(s).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return s
    return s


def security(privacy, akms) -> str:
    kinds = [AKM.get(_int(a), f"AKM {a}") for a in str(akms).split(",") if a.strip()]
    if kinds:
        return "/".join(dict.fromkeys(kinds))
    return "WEP or WPA1" if str(privacy) in ("1", "True") else "Open"


def multicast(mac: str) -> bool:
    try:
        return mac in BROADCAST or bool(int(mac.split(":")[0], 16) & 1)
    except ValueError:
        return True


class Reader:
    def __init__(self):
        self.ap_seen: dict[str, float] = {}
        self.frames = 0
        self.channel = None

    def line(self, raw: str):
        f = raw.rstrip("\n").split("\t")
        if len(f) < len(FIELDS):
            f += [""] * (len(FIELDS) - len(f))
        d = dict(zip(FIELDS, f))
        self.frames += 1
        t = float(d["frame.time_epoch"] or time.time())
        st = _int(d["wlan.fc.type_subtype"], 0)
        sig = _int(d["radiotap.dbm_antsignal"])
        ch = _int(d["wlan.ds.current_channel"]) or _int(d["wlan_radio.channel"])
        bssid = d["wlan.bssid"].split(",")[0]
        sa, da = d["wlan.sa"].split(",")[0], d["wlan.da"].split(",")[0]
        if st in (0x08, 0x05) and bssid:                         # beacon, probe response
            if t - self.ap_seen.get(bssid, 0) >= 5:
                self.ap_seen[bssid] = t
                emit({"event": "ap", "bssid": bssid, "ssid": ssid_text(d["wlan.ssid"]), "channel": ch, "signal": sig,
                      "security": security(d["wlan.fixed.capabilities.privacy"], d["wlan.rsn.akms.type"]), "t": t})
        elif st == 0x04 and sa:                                  # probe request
            emit({"event": "probe", "mac": sa, "ssid": ssid_text(d["wlan.ssid"]), "signal": sig, "t": t})
        elif st is not None and 0x20 <= st <= 0x2F and bssid and bssid not in BROADCAST:   # data
            ds = _int(d["wlan.fc.ds"], 0) or 0
            client = sa if ds == 1 else da if ds == 2 else None
            if client and not multicast(client) and client != bssid:
                emit({"event": "client", "mac": client, "bssid": bssid, "signal": sig if ds == 1 else None, "t": t})
            name = d["tls.handshake.extensions_server_name"] or d["http.host"] or d["dns.qry.name"]
            if client and (name or d["ip.dst"]):
                emit({"event": "traffic", "mac": client, "to": d["ip.dst"] if ds == 1 else d["ip.src"],
                      "name": name.split(",")[0], "t": t})


def tshark_cmd(source_args, key):
    cmd = ["tshark", "-l", "-n", *source_args, "-T", "fields", "-E", "separator=/t", "-E", "occurrence=a",
           "-E", "aggregator=,"]
    for f in FIELDS:
        cmd += ["-e", f]
    if key:
        cmd += ["-o", "wlan.enable_decryption:TRUE", "-o", f'uat:80211_keys:"wpa-pwd","{key}"']
    return cmd


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface")
    ap.add_argument("--pcap")
    ap.add_argument("--key", default="", help="PASSWORD:SSID of your own network, to decrypt it")
    a = ap.parse_args(argv)
    if shutil.which("tshark") is None:
        sys.exit("wifi: tshark isn't installed (the tshark package)")
    r = Reader()
    if a.pcap:
        p = subprocess.Popen(tshark_cmd(["-r", a.pcap], a.key), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        for line in p.stdout:
            r.line(line)
        p.wait()
        return
    # Live: wait for the helper's interface, then tshark on it while the channel reports come in.
    iface = a.iface
    for line in sys.stdin:
        line = line.strip()
        if line.startswith("ERROR"):
            sys.exit("wifi: " + line[6:])
        if line.startswith("IFACE"):
            iface = line.split()[1]
            break
    if not iface:
        sys.exit("wifi: the monitor interface didn't come up (was the password prompt cancelled?)")
    p = subprocess.Popen(tshark_cmd(["-i", iface], a.key), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def frames():
        for line in p.stdout:
            r.line(line)
    threading.Thread(target=frames, daemon=True).start()
    try:
        for line in sys.stdin:
            if line.startswith("CHANNEL"):
                emit({"event": "channel", "channel": r.channel, "frames": r.frames})
                r.channel, r.frames = _int(line.split()[1]), 0
        sys.exit("wifi: the monitor interface's helper stopped")
    finally:
        p.terminate()


if __name__ == "__main__":
    main()
