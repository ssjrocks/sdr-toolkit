"""Satellite tracker: passes over you, and what the satellites transmit (satellites/)."""

import json
import logging
import subprocess
import threading
import time

from .base import Event, Setting, Mode, Tuning
from .radio import LOCATION_SETTINGS, home
from .. import system

log = logging.getLogger(__name__)
NEXT_HOURS = 24                     # the list's "next pass" looks this far ahead
WORKERS = 3                         # processes working out the next passes (satellites/worker.py)
REPORT_EVERY = 1.0                  # s between progress reports while they do
KEEP_LIST = 3600                    # s the catalogue is kept between visits to the page (then re-read: the
                                    # files are downloaded again only when they're due, satellites/data.FRESH)
KEEP_PASSES = 3600                  # s the next passes are kept between visits, for the same place and settings
_kept: dict = {}                    # what the last visit worked out (the page is a new SatelliteMode each time)


class SatelliteMode(Mode):
    id = "satellites"
    name = "Satellite tracker"
    group = "Satellites"
    summary = ("When weather, amateur and space-station satellites pass over you, the best passes, and "
               "everything they transmit: what you can listen to or decode here, and how to tune it.")
    source = "none"                 # no audio, no dongle: the page works out the passes
    output = "satellites"
    folder = None                   # the catalogue's cache (tests use their own)
    offline = False
    settings = LOCATION_SETTINGS + (
        Setting("min_el", "Lowest pass", 10, "int", 0, 60, 5, suffix="°",
                help="Passes whose highest point is below this aren't listed. Low passes are short and "
                     "faint, often behind buildings or trees."),
        Setting("days", "Predict passes for", 5, "int", 1, 10, 1, suffix=" days"),
        Setting("only_hear", "Only satellites I can hear", True, "bool",
                help="Hide satellites with no downlink an RTL-SDR can receive that's still heard (24 MHz "
                     "to 1.77 GHz, as SatNOGS reports it)."),
    )
    tuning = Tuning(
        sdr_mode="Pick a satellite and a downlink on its Radio tab, then Listen: Spectrum & receiver tunes "
                 "there and follows the Doppler shift during the pass.",
        bandwidth="FM voice and APRS 12.5-15 kHz; SSB 2.7 kHz; weather APT 34 kHz",
        frequencies=(("145.800 MHz FM", "ISS voice and SSTV"), ("145.825 MHz", "ISS APRS digipeater"),
                     ("437.800 MHz FM", "ISS cross-band repeater"), ("137.1 / 137.9 MHz", "Meteor LRPT pictures"),
                     ("435-438 MHz", "most amateur cubesats")),
        notes="Satellites are only up for a few minutes a pass, and low ones are faint: a vertical or "
              "turnstile antenna outdoors helps, a V-dipole or QFH for weather satellites. The Doppler "
              "shift is up to ±3.5 kHz on 2 m and ±10 kHz on 70 cm.",
    )

    def start(self, values):
        super().start(values)
        self.catalogue = None
        self.next_passes = {}
        self._events: list = []
        self._halt = threading.Event()
        threading.Thread(target=self._load, daemon=True, name="satellites").start()

    def home(self):
        return home(self.values)

    def _load(self):
        from ..satellites import data
        where = (str(self.folder), self.offline)
        kept = _kept.get("catalogue")
        if kept is not None and kept[0] == where and time.time() - kept[1] < KEEP_LIST:
            cat = kept[2]                                  # read on an earlier visit
        else:
            downloading = not self.offline and data.due(self.folder)
            self._events.append(Event("status", "Updating the satellite list (downloading from SatNOGS, CelesTrak)…"
                                      if downloading else "Loading the satellite list…"))
            try:
                cat = data.load(offline=self.offline, folder=self.folder)
            except Exception as e:  # a bad download mustn't take the window down
                log.exception("satellites")
                self._events.append(Event("status", f"Couldn't get the satellite list: {e}"))
                return
            _kept["catalogue"] = (where, time.time(), cat)
            _kept.pop("passes", None)                      # new elements: passes worked out again
        self.catalogue = cat
        self._events.append(Event("sat_catalogue", data={"catalogue": cat}))
        self._events.append(Event("status", f"{len(cat)} satellites, {sum(s.can_hear for s in cat.values())} "
                                            "you can hear with an RTL-SDR"))
        here = self.home()
        if here is None:
            self._events.append(Event("status", "Set your location (Settings) for passes"))
            return
        # The next passes of every satellite, in processes of their own: a thread here would keep
        # the window's Python busy for seconds. The ones you can listen to come first.
        from ..satellites.worker import to_passes
        now = time.time()
        setup = (tuple(here), float(self.values.get("min_el", 10)), where)
        kept = _kept.get("passes")
        if kept is not None and kept[0] == setup and now - kept[1] < KEEP_PASSES:
            found = {n: [p for p in ps if p.end > now] for n, ps in kept[2].items()}
            self.next_passes = found
            self._events.append(Event("sat_next", data={"passes": found, "done": True}))
            return
        sats = sorted(cat.values(), key=lambda s: (not s.fun, not s.can_hear))
        job = {"home": list(here), "start": now - 1800, "days": NEXT_HOURS / 24 + 1 / 48,
               "min_el": float(self.values.get("min_el", 10)), "step": 60.0}
        found, lock = {}, threading.Lock()
        reported = [0.0]

        def read(proc):
            for line in proc.stdout:
                try:
                    norad, rows = json.loads(line)
                except ValueError:
                    continue
                with lock:
                    found[norad] = [p for p in to_passes(rows) if p.end > now]
                    if time.time() - reported[0] >= REPORT_EVERY:
                        reported[0] = time.time()
                        self.next_passes = dict(found)
                        self._events.append(Event("sat_next", data={"passes": self.next_passes, "done": False}))

        self._procs = []
        readers = []
        for k in range(WORKERS):
            part = [[s.norad, s.line1, s.line2] for s in sats[k::WORKERS]]
            proc = subprocess.Popen(system.python_command("sdr_toolkit.satellites.worker"), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, text=True)
            self._procs.append(proc)
            proc.stdin.write(json.dumps(dict(job, sats=part)))
            proc.stdin.close()
            t = threading.Thread(target=read, args=(proc,), daemon=True)
            t.start()
            readers.append(t)
        for t in readers:
            t.join()
        for proc in self._procs:
            proc.wait()
        if self._halt.is_set():
            return
        _kept["passes"] = (setup, now, found)
        self.next_passes = found
        self._events.append(Event("sat_next", data={"passes": found, "done": True}))

    def poll(self):
        events, self._events = self._events, []
        return events

    def stop(self):
        halt = getattr(self, "_halt", None)
        if halt is not None:
            halt.set()
        for proc in getattr(self, "_procs", []):
            if proc.poll() is None:
                proc.terminate()
        return []
