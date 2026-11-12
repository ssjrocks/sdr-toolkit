"""What DSD-FME's output means, in plain words.

DSD-FME prints a line for each frame it locks on to ("Sync: +DMR  [SLOT1]  slot2  | Color Code=01
| VC1", "Sync: +P25p1 ... NAC: 293; ... TSBK"), a line for each call ("SLOT 1 TGT=101 SRC=3120045
Group Call", sometimes "Encrypted"), and lines for a trunked system's control messages (channel
grants, network status, Capacity Plus / Connect Plus). parse() turns a line into a dict; Channel
keeps what one frequency has shown and says what it is ("P25 control channel", "DMR voice
channel: a call on slot 1", "DMR repeater, idle"), with a sentence for someone who's never heard of
colour codes or NACs.
"""

import collections
import re
import time

ANSI = re.compile(r"\x1b\[[0-9;]*m")
SYNC = re.compile(r"Sync:\s*([+-]?)\s*(DMR|P25p1|P25p2|NXDN48|NXDN96|NXDN|DSTAR\S*|YSF|dPMR|DPMR|M17|EDACS\S*|"
                  r"X2-TDMA|ProVoice)\b")
CC = re.compile(r"Color Code=(\d+)")
NAC = re.compile(r"NAC(?:/CC)?:\s*([0-9A-Fa-f]+)")
SLOT = re.compile(r"\[SLOT([12])\]")
CALL = re.compile(r"SLOT (\d)\s+TGT=(\d+) SRC=(\d+)\s*(.*)")
P25_CALL = re.compile(r"TGT:\s*(\d+);\s*SRC:\s*(\d+)")
POSITION = re.compile(r"Lat:\s*(-?\d+\.\d+)\s*Lon:\s*(-?\d+\.\d+)")
TEXT = re.compile(r"(?:UTF8|UTF16|ISO7|ISO8)? ?Text:\s*(.*)")
DATA_WORDS = (("LRRP", "GPS report (LRRP)"), ("Short Data", "short data"), ("Data Header", "data header"),
              ("Response Packet", "data acknowledgement"), ("UDT", "unified data"), ("IP Packet", "IP data"),
              ("MNIS", "MNIS data"), ("Proprietary", "proprietary data"))
SYSINFO = re.compile(r"WACN \[([0-9A-F]+)\] SYSID \[([0-9A-F]+)\] NAC \[([0-9A-F]+)\]")

# DMR burst types after "| " in a sync line (DSD-FME's names) -> what they carry
DMR_KINDS = {"VC": "voice", "VLC": "voice", "TLC": "voice", "CSBK": "control", "MBC": "control",
             "IDLE": "idle", "DATA": "data", "PI": "voice", "R12": "data", "R34": "data", "R1": "data",
             "UDT": "data", "USB": "data", "CACH": "control"}
P25_KINDS = {"HDU": "voice", "LDU1": "voice", "LDU2": "voice", "TDU": "voice", "TDULC": "voice",
             "TSBK": "control", "MPDU": "data", "PDU": "data"}
CONTROL_WORDS = ("Channel Grant", "Network Status", "Adjacent", "Aloha", "Announcement", "C_BCAST",
                 "Rest Channel", "Rest LSN", "Channel Status", "Registration", "RFSS Status", "IDEN_UP")
SYSTEM_WORDS = (("Capacity Plus", "Motorola Capacity Plus"), ("Connect Plus", "Motorola Connect Plus"),
                ("Hytera XPT", "Hytera XPT"), ("Tier III", "DMR Tier III"))


def parse(line: str) -> dict | None:
    """One DSD-FME line: {"what": "frame" | "call" | "control" | "system", ...}, or None."""
    line = ANSI.sub("", line).strip()
    if not line:
        return None
    m = SYNC.search(line)
    if m:
        proto = m.group(2)
        out = {"what": "frame", "proto": proto, "encrypted": "ENC" in line.upper() and "ENCRYPTED" in line.upper()}
        slot = SLOT.search(line)
        if slot:
            out["slot"] = int(slot.group(1))
        cc = CC.search(line)
        if cc:
            out["cc"] = int(cc.group(1))                     # decoded and passed its checks: verified
        nac = NAC.search(line)
        if nac:
            out["nac"] = nac.group(1).upper()
        tail = line.rsplit("|", 1)[-1] if "|" in line else line
        kinds = P25_KINDS if proto.startswith("P25") else DMR_KINDS
        for word in tail.replace(";", " ").split():
            word = word.strip("*").upper()
            for name, kind in kinds.items():
                if word == name or (name in ("VC",) and word.startswith("VC") and word[2:].isdigit()):
                    out["kind"] = kind
                    break
            if "kind" in out:
                break
        if "Err" in line or "ERR" in line:
            out["error"] = True
        return out
    m = CALL.search(line)
    if m:
        rest = m.group(4)
        return {"what": "call", "slot": int(m.group(1)), "tgt": int(m.group(2)), "src": int(m.group(3)),
                "group": "Private" not in rest, "encrypted": "Encrypted" in rest, "emergency": "Emergency" in rest,
                "all": "All " in rest}
    m = P25_CALL.search(line)
    if m and ("Group" in line or "Unit" in line or "Voice" in line):
        return {"what": "call", "slot": 0, "tgt": int(m.group(1)), "src": int(m.group(2)),
                "group": "Unit to Unit" not in line, "encrypted": "Encrypted" in line, "emergency": "Emergency" in line,
                "all": False}
    m = POSITION.search(line)
    if m:
        return {"what": "position", "lat": float(m.group(1)), "lon": float(m.group(2))}
    m = TEXT.search(line)
    if m and m.group(1).strip():
        return {"what": "text", "text": m.group(1).strip()}
    for word, name in DATA_WORDS:
        if word in line:
            return {"what": "data", "name": name}
    m = SYSINFO.search(line)
    if m:
        return {"what": "system", "wacn": m.group(1), "sysid": m.group(2), "nac": m.group(3)}
    for word, name in SYSTEM_WORDS:
        if word in line:
            return {"what": "control", "text": line, "name": _control_name(line), "system": name}
    if any(w in line for w in CONTROL_WORDS):
        return {"what": "control", "text": line, "name": _control_name(line)}
    return None


def _control_name(line) -> str:
    """A control message's name: "Talkgroup Voice Channel Grant (TV_GRANT)" from its line."""
    name = re.split(r"[;:\-]\s", line.strip(), maxsplit=1)[0].strip()
    return name[:60]


PROTO_NAMES = {"DMR": "DMR", "P25p1": "P25 phase 1", "P25p2": "P25 phase 2", "NXDN48": "NXDN", "NXDN96": "NXDN",
               "DSTAR": "D-Star", "YSF": "Yaesu System Fusion", "DPMR": "dPMR", "M17": "M17", "EDACS": "EDACS",
               "X2-TDMA": "X2-TDMA", "ProVoice": "ProVoice"}

EXPLAIN = {
    "cc": ("The colour code (0-15) is DMR's channel access code, like a CTCSS tone for analogue radios: "
           "radios ignore a repeater with a different one. Colour code 0 and 1 are common defaults."),
    "nac": ("The NAC (Network Access Code) is P25's channel access code; a system's sites often share one, "
            "and it tells systems apart."),
    "control": ("A control channel carries no voice: it tells radios on a trunked system which channel each "
                "call is on. The calls themselves are on other frequencies nearby."),
    "voice": "A voice channel carries calls: talkgroup (who it's for) and radio ID (who's talking).",
    "idle": "An idle DMR repeater sends a beacon now and then so radios can find it; calls come when someone talks.",
    "data": "Data: GPS positions, text messages or telemetry, not voice.",
    "slot": ("DMR splits each frequency into two time slots, so a repeater carries two calls at once (slot 1 "
             "and slot 2)."),
    "encrypted": "Encrypted: the voice can't be heard without the key (the toolkit doesn't take keys).",
}


class Channel:
    """What one frequency has shown: protocol, access code, kinds of traffic, calls, and what it is."""

    def __init__(self, freq):
        self.freq = freq
        self.proto = ""
        self.cc: collections.Counter = collections.Counter()
        self.nac: collections.Counter = collections.Counter()
        self.kinds: collections.Counter = collections.Counter()
        self.slots: collections.Counter = collections.Counter()
        self.system = ""
        self.sysinfo: dict = {}
        self.calls = 0
        self.encrypted_calls = 0
        self.last = 0.0
        self.last_call: dict | None = None
        self.last_call_t = 0.0
        self.frames = 0
        self.decoded: collections.Counter = collections.Counter()   # what the data carried: "GPS positions"...
        self.positions: list = []                                     # (time, lat, lon)
        self.texts: list = []                                         # (time, text)
        self.controls: collections.Counter = collections.Counter()   # control messages by name

    def add(self, e: dict, now=None):
        now = time.time() if now is None else now
        self.last = now
        w = e["what"]
        if w == "frame":
            self.frames += 1
            self.proto = e["proto"]
            if "cc" in e:
                self.cc[e["cc"]] += 1
            if "nac" in e:
                self.nac[e["nac"]] += 1
            if "kind" in e:
                self.kinds[e["kind"]] += 1
            if "slot" in e:
                self.slots[e["slot"]] += 1
        elif w == "call":
            same = (self.last_call and self.last_call["tgt"] == e["tgt"] and self.last_call["src"] == e["src"]
                    and now - self.last_call_t < 5)
            if not same:
                self.calls += 1
                self.encrypted_calls += bool(e["encrypted"])
            self.last_call, self.last_call_t = e, now
            self.kinds["voice"] += 1
        elif w == "system":
            self.sysinfo = {k: e[k] for k in ("wacn", "sysid", "nac")}
            self.kinds["control"] += 1
        elif w == "control":
            self.kinds["control"] += 1
            if e.get("system"):
                self.system = e["system"]
            self.controls[e.get("name") or "control message"] += 1
        elif w == "position":
            self.decoded["GPS positions"] += 1
            self.positions = (self.positions + [(now, e["lat"], e["lon"])])[-200:]
        elif w == "text":
            self.decoded["text messages"] += 1
            self.texts = (self.texts + [(now, e["text"])])[-50:]
        elif w == "data":
            self.decoded[e["name"]] += 1

    @property
    def calling(self) -> bool:
        return self.last_call is not None and time.time() - self.last_call_t < 3

    def name(self) -> str:
        return PROTO_NAMES.get(self.proto, self.proto or "Digital")

    def what(self) -> str:
        """What this channel is, in a few words."""
        if not self.kinds and not self.proto:
            return "signal: no digital voice heard yet"
        k = self.kinds
        main = k.most_common(1)[0][0] if k else ""
        proto = self.name()
        if main == "control" or (k["control"] > 2 * max(k["voice"], 1) and k["control"] >= 3):
            sysname = f" ({self.system})" if self.system else ""
            return f"{proto} control channel{sysname}"
        if main == "voice" or k["voice"]:
            enc = ", encrypted" if self.calls and self.encrypted_calls == self.calls else ""
            return f"{proto} voice channel{enc}"
        if main == "idle":
            return f"{proto} repeater, idle"
        if main == "data":
            return f"{proto} data channel"
        return f"{proto} signal"

    def code(self) -> str:
        if self.cc:
            return f"colour code {self.cc.most_common(1)[0][0]}"
        if self.nac:
            return f"NAC {self.nac.most_common(1)[0][0]}"
        return ""

    def explain(self) -> str:
        """A few sentences on what this is, for someone new to it."""
        parts = []
        k = self.kinds
        main = k.most_common(1)[0][0] if k else ""
        if "control" in self.what():
            parts.append(EXPLAIN["control"])
        elif k["voice"]:
            parts.append(EXPLAIN["voice"])
        elif main in EXPLAIN:
            parts.append(EXPLAIN[main])
        if self.proto == "DMR" and self.slots:
            parts.append(EXPLAIN["slot"])
        if self.cc:
            parts.append(EXPLAIN["cc"])
        if self.nac:
            parts.append(EXPLAIN["nac"])
        if self.encrypted_calls:
            parts.append(EXPLAIN["encrypted"])
        return " ".join(parts)

    def contents(self) -> str:
        """What the frames carried, as far as DSD-FME decoded them: "12 GPS positions, 2 text messages,
        40 control messages (most: Talkgroup Voice Channel Grant)"."""
        parts = [f"{n} {name}" for name, n in self.decoded.most_common()]
        if self.controls:
            n = sum(self.controls.values())
            parts.append(f"{n} control message{'s' if n != 1 else ''} (most: {self.controls.most_common(1)[0][0]})")
        if self.calls:
            parts.insert(0, f"{self.calls} call{'s' if self.calls != 1 else ''}"
                            + (f" ({self.encrypted_calls} encrypted)" if self.encrypted_calls else ""))
        return ", ".join(parts)

    def line(self, now=None) -> str:
        """A one-line description for a log: "DMR voice channel, colour code 1, slots 1 and 2"."""
        bits = [self.what()]
        if self.code():
            bits.append(self.code())
        if self.slots:
            bits.append(" and ".join(f"slot {s}" for s in sorted(self.slots)))
        if self.calling and self.last_call:
            c = self.last_call
            bits.append(f"{'group' if c['group'] else 'private'} call to {c['tgt']} from radio {c['src']}"
                        + (" (encrypted)" if c["encrypted"] else ""))
        return ", ".join(bits)
