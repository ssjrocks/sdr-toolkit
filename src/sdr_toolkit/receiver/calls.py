"""Single transmissions (calls) the explorer recorded: one WAV each.

Each decode point's DSD-FME saves every call it decodes as its own 8 kHz WAV (its -P option) in a
folder named after the point's frequency, under the calls folder. DSD-FME names the file after the
call: "20261113_123000_05682__GROUP_TGT_505_SRC_3125678.wav" (date, time, a number, the kind of
call, talkgroup, radio). A call is listed once its file has stopped growing; the network its channel
belongs to (from the explorer) is noted then, in an index beside the folders, so it stays with the
call after the explorer's lists change or the app restarts.
"""

import json
import re
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

# "20261113_133614_20467_DMR_CC_1_GROUP_TGT_10140027_SRC_140205.wav": the protocol, colour code and kind
# of call are there when DSD-FME knew them ("DMR_CC_1_", or nothing at all, otherwise).
NAME = re.compile(r"^(\d{8})_(\d{6})_(\d+)_(.*?)_*TGT_(\d+)_SRC_(\d+)", re.I)
PROTOS = {"DMR": "DMR", "P25": "P25", "NXDN": "NXDN", "DSTAR": "D-Star", "YSF": "YSF", "DPMR": "dPMR", "M17": "M17"}
SETTLE = 3.0                    # s a file must be unchanged for its call to be over
SAME_CALL = 2.0                 # s: the same talkgroup and radio this close in time is the same transmission
RECENT = 600.0                  # s of listed calls a new one is checked against for being a copy
INDEX = ".sdr-toolkit-calls.json"


def folder_for(root, freq) -> Path:
    """Where a decode point on freq (Hz) saves its calls."""
    return Path(root).expanduser() / f"{freq / 1e6:.5f} MHz"


@dataclass
class Call:
    path: str
    freq: float                 # Hz
    start: float                # unix time
    length: float               # s
    kind: str                   # "group", "private", or what DSD-FME called it
    talkgroup: int | None
    radio: int | None
    network: str = ""

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def end(self) -> float:
        return self.start + self.length

    def same_as(self, other) -> bool:
        """The same transmission heard on another channel (a network sends each call out on several
        sites at once): the same talkgroup and radio, at overlapping times. On one channel, files
        that follow each other are pieces of a call DSD-FME lost and found again, not copies. Calls
        from an unknown radio aren't matched."""
        return (bool(self.radio) and self.radio == other.radio and self.talkgroup == other.talkgroup
                and abs(self.freq - other.freq) > 1000 and self.start < other.end + SAME_CALL
                and other.start < self.end + SAME_CALL)


def parse_name(name: str) -> dict:
    """What DSD-FME put in a call file's name: kind of call, talkgroup, radio, and the network it knew
    ("DMR · colour code 1", as the explorer names one)."""
    m = NAME.match(name)
    if not m:
        return {"kind": "", "talkgroup": None, "radio": None, "named": ""}
    mid = m.group(4).upper()
    kind = "group" if "GROUP" in mid else "private" if "PRIV" in mid else ""
    proto = PROTOS.get(mid.split("_")[0], "")
    cc = re.search(r"CC_(\d+)", mid)
    nac = re.search(r"NAC_([0-9A-F]+)", mid)
    named = (f"{proto} · colour code {int(cc.group(1))}" if proto and cc else
             f"{proto} · NAC {nac.group(1)}" if proto and nac else "")
    return {"kind": kind, "talkgroup": int(m.group(5)), "radio": int(m.group(6)), "named": named}


def wav_length(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (OSError, EOFError, wave.Error):
        return None


class CallLog:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.calls: dict[str, Call] = {}            # path -> call
        self._index: dict[str, dict] = {}            # file (relative to root) -> what was known when it was found
        self._load_index()

    def _load_index(self):
        try:
            self._index = json.loads((self.root / INDEX).read_text())
        except (OSError, ValueError):
            self._index = {}

    def _save_index(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / (INDEX + ".part")
            tmp.write_text(json.dumps(self._index))
            tmp.replace(self.root / INDEX)
        except OSError:
            pass

    def folder_for(self, freq) -> Path:
        return folder_for(self.root, freq)

    @staticmethod
    def _freq(folder: Path) -> float | None:
        try:
            return float(folder.name.split()[0]) * 1e6
        except (ValueError, IndexError):
            return None

    def scan(self, network_of=lambda f: "", now=None) -> tuple[list[Call], list[str]]:
        """The calls that have finished since the last scan (oldest first), and the paths of calls
        listed before that are gone now (a better copy of the same transmission came in)."""
        now = time.time() if now is None else now
        new = []
        changed = False
        try:
            folders = [d for d in self.root.iterdir() if d.is_dir()]
        except OSError:
            return [], []
        for d in folders:
            freq = self._freq(d)
            if freq is None:
                continue
            try:
                files = list(d.glob("*.wav"))
            except OSError:
                continue
            for f in files:
                key = str(f)
                if key in self.calls:
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if now - st.st_mtime < SETTLE or st.st_size <= 44:
                    continue                          # still being written, or nothing in it
                length = wav_length(f)
                if length is None:
                    continue
                rel = f"{d.name}/{f.name}"
                known = self._index.get(rel)
                if known is None:
                    known = self._index[rel] = {"network": network_of(freq), "start": st.st_mtime - length}
                    changed = True
                info = parse_name(f.name)
                named = info.pop("named")
                call = Call(key, freq, float(known.get("start", st.st_mtime - length)), length,
                            network=known.get("network") or named, **info)
                self.calls[key] = call
                new.append(call)
        removed = self._keep_best(sorted(new, key=lambda c: c.start), now)
        if changed or removed:
            self._save_index()
        new = [c for c in new if c.path in self.calls]
        return sorted(new, key=lambda c: c.start), removed

    def _keep_best(self, new, now) -> list[str]:
        """Copies of one transmission: only the longest is kept (the one heard best: DSD-FME stops a
        call's file when it loses the signal), the others' files are deleted. Returns the paths of
        calls listed before that were dropped for a better copy."""
        fresh = {c.path for c in new}
        removed = []
        for c in new:
            if c.path not in self.calls:
                continue                              # dropped already, for a copy before it
            for other in list(self.calls.values()):
                if other.path == c.path or now - other.end > RECENT or not c.same_as(other):
                    continue
                worse = c if (c.length, c.path) < (other.length, other.path) else other
                self._drop(worse)
                if worse.path not in fresh:
                    removed.append(worse.path)
                if worse is c:
                    break
        return removed

    def _drop(self, call):
        self.calls.pop(call.path, None)
        p = Path(call.path)
        self._index.pop(f"{p.parent.name}/{p.name}", None)
        try:
            p.unlink()
        except OSError:
            pass

    def to_dict(self, call: Call) -> dict:
        return asdict(call)
