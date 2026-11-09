"""What every mode provides: settings, tuning help, and a decoder fed with audio.

The window knows nothing about any mode. It builds the settings form from
``settings``, shows ``tuning`` as instructions, records audio at ``rate`` and
passes it to ``feed``; the mode answers with events the window displays.
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    default: Any
    kind: str = "float"            # "int", "float", "choice", "bool", "text"
    minimum: float = 0
    maximum: float = 0
    step: float = 1
    decimals: int = 0
    suffix: str = ""
    choices: tuple = ()            # (value, label) pairs for "choice"
    help: str = ""
    restart: bool = False          # the decoder must restart to apply it
    shared: bool = False           # one value for every mode that has it (the RTL-SDR's ppm...)


@dataclass(frozen=True)
class Tuning:
    """How to set up the receiver for this mode."""

    sdr_mode: str                  # what to select on the SDR / WebSDR
    bandwidth: str
    frequencies: tuple = ()        # (where, what) pairs
    notes: str = ""


@dataclass
class Event:
    """Something for the window to show.

    kind:
      "line"    a finished line of text (``text``, optional ``style``)
      "live"    the line being received now, replaced each time (``text``)
      "row"     a table row (``data``: dict of column -> value)
      "image"   picture update (``image``: pixels HxWx3, ``title``, ``done``,
                ``row``: the row being drawn, or None)
      "status"  a short note for the status line (``text``)
      "activity" what a radio mode is doing now, shown above its output (``text``)
      "log"     a line for the decoded-data log only (modes whose table updates in place)
      "raw"     the decoder's raw output (an outside program's line, NMEA, frame bytes...)
      "points"  map markers (``data["points"]``: list of dicts with id, lat, lon,
                label, heading, kind), for modes with ``map = True``
    """

    kind: str
    text: str = ""
    style: str = ""                 # "", "dim", "warn"
    data: dict = field(default_factory=dict)
    image: np.ndarray | None = None
    title: str = ""
    done: bool = False
    row: int | None = None


class MissingParts(RuntimeError):
    """A mode needs outside parts that aren't installed (keys of sdr_toolkit.deps.PARTS)."""

    def __init__(self, parts):
        self.parts = list(parts)
        super().__init__("needs " + ", ".join(self.parts))


class Mode:
    id = ""
    name = ""
    group = ""
    summary = ""
    rate = 12000                    # audio sample rate the decoder wants
    output = "text"                 # "text", "table" or "image"
    columns: tuple = ()             # for "table": (key, heading) pairs
    settings: tuple[Setting, ...] = ()
    tuning: Tuning = Tuning("", "")
    requires: tuple = ()            # outside programs needed, e.g. ("jt9",)

    def __init__(self):
        self.values = {s.key: s.default for s in self.settings}

    # The window calls these.

    def start(self, values: dict) -> None:
        """Start decoding with these setting values."""
        self.values = dict(values)

    def apply(self, values: dict) -> None:
        """Settings changed while running (those without restart=True)."""
        self.values = dict(values)

    def feed(self, audio: np.ndarray, now: float) -> list[Event]:
        """Audio at ``rate``, mono float32; *now* is time.time() at its end."""
        return []

    def poll(self) -> list[Event]:
        """Events from background work (outside programs), called every tick."""
        return []

    def stop(self) -> list[Event]:
        """Stop; return anything still pending (a half-received picture)."""
        return []

    def markers(self) -> list[tuple[float, str]]:
        """Audio frequencies to mark on the spectrum: (Hz, label)."""
        return []

    def set_frequency(self, hz: float) -> str | None:
        """The user clicked the spectrum at *hz*. Return the setting key changed, if any."""
        return None
