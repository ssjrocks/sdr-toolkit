"""The digital voice explorer's picture of an area: every channel found, what each is, the systems
they belong to, and the talkgroups and radios heard on them.

Fed with DSD-FME's parsed lines (receiver/dsd.py) per frequency. Kept between sessions as JSON,
so exploring builds up a map of what's on the air. It also decides what to listen to: everything,
or only one channel, one talkgroup or one radio (the others are muted while their calls are on).
"""

import json
import time
from pathlib import Path

from .dsd import Channel


class Explorer:
    def __init__(self):
        self.channels: dict[float, Channel] = {}
        self.states: dict[float, str] = {}           # freq -> "probing", "digital", "not digital", "idle"
        self.talkgroups: dict[int, dict] = {}
        self.radios: dict[int, dict] = {}
        self.only = None                              # None (everything), ("freq", Hz), ("tg", n) or ("radio", n)
        self.log: list = []                           # (time, freq, text): what happened, newest last

    # ---- what was heard --------------------------------------------------------------------

    def channel(self, freq) -> Channel:
        ch = self.channels.get(freq)
        if ch is None:
            ch = self.channels[freq] = Channel(freq)
        return ch

    def add(self, freq, e: dict, now=None) -> str | None:
        """One parsed DSD-FME line on freq. Returns a line for the activity log, if it's news."""
        now = time.time() if now is None else now
        ch = self.channel(freq)
        before = ch.what()
        ch.add(e, now)
        self.states[freq] = "digital"
        news = None
        if e["what"] == "call":
            tg = self.talkgroups.setdefault(e["tgt"], {"calls": 0, "radios": set(), "freqs": set(), "first": now,
                                                       "group": e["group"], "encrypted": 0, "proto": ch.proto})
            new_call = not (tg.get("last_src") == e["src"] and now - tg.get("last", 0) < 5)
            tg.update(last=now, last_src=e["src"], last_freq=freq)
            tg["radios"].add(e["src"])
            tg["freqs"].add(freq)
            r = self.radios.setdefault(e["src"], {"calls": 0, "talkgroups": set(), "freqs": set(), "first": now,
                                                  "proto": ch.proto})
            r.update(last=now, last_tg=e["tgt"], last_freq=freq)
            r["talkgroups"].add(e["tgt"])
            r["freqs"].add(freq)
            if new_call:
                tg["calls"] += 1
                r["calls"] += 1
                tg["encrypted"] += bool(e["encrypted"])
                news = (f"{'Group' if e['group'] else 'Private'} call to {e['tgt']} from radio {e['src']}"
                        + (f", slot {e['slot']}" if e.get("slot") else "") + (", encrypted" if e["encrypted"] else "")
                        + (", EMERGENCY" if e["emergency"] else ""))
        elif e["what"] == "position":
            src = ch.last_call["src"] if ch.last_call and now - ch.last_call_t < 30 else None
            if src is not None:
                self.radios.setdefault(src, {"calls": 0, "talkgroups": set(), "freqs": set(), "first": now,
                                             "proto": ch.proto}).update(position=(e["lat"], e["lon"]), position_t=now)
            news = f"GPS position {e['lat']:.5f}, {e['lon']:.5f}" + (f" (radio {src})" if src else "")
        elif e["what"] == "text":
            news = f"Text message: {e['text']}"
        if news is None and ch.what() != before and ch.frames >= 3:
            news = f"{ch.line()}"
        if news:
            self.log = (self.log + [(now, freq, news)])[-2000:]
        return news

    def found(self, freq, state, now=None):
        """The receiver found a signal, or gave up on one (not digital voice), or let one go idle."""
        now = time.time() if now is None else now
        if self.states.get(freq) == "digital" and state == "probing":
            return
        self.states[freq] = state
        if state == "not digital":
            self.log = (self.log + [(now, freq, "Not digital voice (DSD-FME found nothing it knows)")])[-2000:]
        elif state == "probing" and freq not in self.channels:
            self.log = (self.log + [(now, freq, "New signal: listening for digital voice")])[-2000:]

    # ---- systems: channels that belong together ---------------------------------------------

    def systems(self) -> list[dict]:
        """Channels grouped by protocol and access code (and a P25 system's WACN/SYSID): a DMR
        repeater or trunked site, a P25 system."""
        out: dict = {}
        for f, ch in self.channels.items():
            if not ch.proto:
                continue
            if ch.sysinfo:
                key = (ch.proto, f"WACN {ch.sysinfo['wacn']} SYSID {ch.sysinfo['sysid']}")
            else:
                key = (ch.proto, ch.code() or "no access code yet")
            s = out.setdefault(key, {"proto": ch.name(), "id": key[1], "freqs": [], "control": [], "voice": [],
                                     "system": "", "calls": 0, "last": 0.0})
            s["freqs"].append(f)
            what = ch.what()
            if "control" in what:
                s["control"].append(f)
            elif "voice" in what:
                s["voice"].append(f)
            s["system"] = s["system"] or ch.system
            s["calls"] += ch.calls
            s["last"] = max(s["last"], ch.last)
        return sorted(out.values(), key=lambda s: -s["last"])

    # ---- networks: what keeps several systems heard at once apart --------------------------------

    def channel_near(self, freq, within=50.0):
        """The channel at freq (a call's folder names it to 10 Hz), if there is one."""
        ch = self.channels.get(freq)
        if ch is None:
            near = [f for f in self.channels if abs(f - freq) <= within]
            ch = self.channels[min(near, key=lambda f: abs(f - freq))] if near else None
        return ch

    def network_of(self, freq) -> str:
        """The network a channel belongs to, in words: its protocol and access code (a DMR colour
        code, a P25 NAC), or a P25 system's WACN and system ID. "" until it's known."""
        ch = self.channel_near(freq)
        if ch is None or not ch.proto:
            return ""
        if ch.sysinfo:
            return f"{ch.name()} · WACN {ch.sysinfo['wacn']} SYSID {ch.sysinfo['sysid']}"
        return f"{ch.name()} · {ch.code() or 'no access code yet'}"

    def networks(self) -> list[str]:
        return sorted({n for f in self.channels if (n := self.network_of(f))})

    def network_freqs(self, network) -> set:
        return {f for f in self.channels if self.network_of(f) == network}

    # ---- listening ----------------------------------------------------------------------------

    def audible(self, freq, now=None) -> bool:
        """Whether freq's audio should be heard under the "listen only to" choice."""
        now = time.time() if now is None else now
        if self.only is None:
            return True
        kind, value = self.only
        if kind == "freq":
            return abs(freq - value) < 1
        ch = self.channels.get(freq)
        if ch is None or ch.last_call is None or now - ch.last_call_t > 3:
            return False                                 # no call on now: nothing to let through
        return ch.last_call["tgt" if kind == "tg" else "src"] == value

    # ---- kept between sessions ----------------------------------------------------------------

    def to_json(self) -> dict:
        def chan(ch):
            return {"proto": ch.proto, "cc": dict(ch.cc), "nac": dict(ch.nac), "kinds": dict(ch.kinds),
                    "slots": dict(ch.slots), "system": ch.system, "sysinfo": ch.sysinfo, "calls": ch.calls,
                    "encrypted_calls": ch.encrypted_calls, "last": ch.last, "frames": ch.frames,
                    "decoded": dict(ch.decoded), "controls": dict(ch.controls)}

        def plain(d):
            return {k: (sorted(v) if isinstance(v, set) else v) for k, v in d.items()}
        return {"channels": {str(f): chan(ch) for f, ch in self.channels.items()},
                "states": {str(f): s for f, s in self.states.items()},
                "talkgroups": {str(k): plain(v) for k, v in self.talkgroups.items()},
                "radios": {str(k): plain(v) for k, v in self.radios.items()},
                "log": self.log[-300:]}

    @classmethod
    def from_json(cls, data: dict) -> "Explorer":
        import collections
        ex = cls()
        for f, c in data.get("channels", {}).items():
            ch = ex.channel(float(f))
            ch.proto, ch.system, ch.sysinfo = c.get("proto", ""), c.get("system", ""), c.get("sysinfo", {})
            for name in ("cc", "nac", "kinds", "slots", "decoded", "controls"):
                setattr(ch, name, collections.Counter({(int(k) if name in ("cc", "slots") else k): v
                                                       for k, v in c.get(name, {}).items()}))
            ch.calls, ch.encrypted_calls = c.get("calls", 0), c.get("encrypted_calls", 0)
            ch.last, ch.frames = c.get("last", 0.0), c.get("frames", 0)
        ex.states = {float(f): s for f, s in data.get("states", {}).items()}
        for name in ("talkgroups", "radios"):
            table = getattr(ex, name)
            for k, v in data.get(name, {}).items():
                table[int(k)] = {a: (set(b) if a in ("radios", "talkgroups", "freqs") else
                                     tuple(b) if a == "position" else b) for a, b in v.items()}
        ex.log = [tuple(x) for x in data.get("log", [])]
        return ex

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_text(json.dumps(self.to_json()))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Explorer":
        try:
            return cls.from_json(json.loads(path.read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            return cls()
