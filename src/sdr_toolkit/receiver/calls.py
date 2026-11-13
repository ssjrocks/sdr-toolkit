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

NAME = re.compile(r"^(\d{8})_(\d{6})_(\d+)_+([A-Z]+)_TGT_(\d+)_SRC_(\d+)", re.I)
SETTLE = 3.0                    # s a file must be unchanged for its call to be over
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


def parse_name(name: str) -> dict:
    """What DSD-FME put in a call file's name."""
    m = NAME.match(name)
    if not m:
        return {"kind": "", "talkgroup": None, "radio": None}
    kind = m.group(4).lower()
    return {"kind": {"grp": "group", "priv": "private"}.get(kind, kind), "talkgroup": int(m.group(5)),
            "radio": int(m.group(6))}


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

    def scan(self, network_of=lambda f: "", now=None) -> list[Call]:
        """The calls that have finished since the last scan, oldest first."""
        now = time.time() if now is None else now
        new = []
        changed = False
        try:
            folders = [d for d in self.root.iterdir() if d.is_dir()]
        except OSError:
            return []
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
                call = Call(key, freq, float(known.get("start", st.st_mtime - length)), length,
                            network=known.get("network", ""), **parse_name(f.name))
                self.calls[key] = call
                new.append(call)
        if changed:
            self._save_index()
        return sorted(new, key=lambda c: c.start)

    def to_dict(self, call: Call) -> dict:
        return asdict(call)
