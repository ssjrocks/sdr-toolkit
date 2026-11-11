"""Satellite tracker: passes over you, and what the satellites transmit (satellites/)."""

import logging
import threading
import time

from .base import Event, Setting, Mode, Tuning
from .radio import LOCATION_SETTINGS, home

log = logging.getLogger(__name__)
NEXT_HOURS = 24                     # the list's "next pass" looks this far ahead


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
        from ..satellites import data, orbit
        self._events.append(Event("status", "Getting the satellite list (SatNOGS, CelesTrak)…"))
        try:
            cat = data.load(offline=self.offline, folder=self.folder)
        except Exception as e:  # a bad download mustn't take the window down
            log.exception("satellites")
            self._events.append(Event("status", f"Couldn't get the satellite list: {e}"))
            return
        self.catalogue = cat
        self._events.append(Event("sat_catalogue", data={"catalogue": cat}))
        self._events.append(Event("status", f"{len(cat)} satellites, {sum(s.can_hear for s in cat.values())} "
                                            "you can hear with an RTL-SDR"))
        here = self.home()
        if here is None:
            self._events.append(Event("status", "Set your location (Settings) for passes"))
            return
        now = time.time()
        found = {}
        for i, sat in enumerate(sorted(cat.values(), key=lambda s: (not s.fun, not s.can_hear))):
            if self._halt.is_set():
                return
            try:
                ps = orbit.passes(orbit.satrec(sat.line1, sat.line2), here, now - 1800, NEXT_HOURS / 24 + 1 / 48,
                                  float(self.values.get("min_el", 10)), step=60.0)
            except Exception:  # elements SGP4 can't use (decayed): no passes
                ps = []
            found[sat.norad] = [p for p in ps if p.end > now]
            if i % 100 == 99:
                self.next_passes = dict(found)
                self._events.append(Event("sat_next", data={"passes": self.next_passes, "done": False}))
        self.next_passes = found
        self._events.append(Event("sat_next", data={"passes": found, "done": True}))

    def poll(self):
        events, self._events = self._events, []
        return events

    def stop(self):
        halt = getattr(self, "_halt", None)
        if halt is not None:
            halt.set()
        return []
