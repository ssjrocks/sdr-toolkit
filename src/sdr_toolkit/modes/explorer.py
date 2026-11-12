"""DMR / P25 explorer: Spectrum & receiver that finds digital voice signals by itself.

Every signal that appears in view gets a decode point running DSD-FME (receiver.py's discovery);
what it hears builds the explorer's lists (receiver/explore.py): channels and what each one is,
systems, talkgroups and radios. Listen to everything at once, or single out a channel, a talkgroup
or a radio.
"""

from dataclasses import replace

from .base import Setting, Tuning
from .receiver import ReceiverMode

DEFAULTS = {"freq_mhz": 438.8, "demod": "NFM", "bandwidth": 12500, "mute": True, "auto_id": False, "decoder": ""}


class ExplorerMode(ReceiverMode):
    id = "explorer"
    name = "DMR / P25 / digital voice"
    group = "RTL-SDR"
    summary = ("Explore digital voice around you: signals in view are found, tried with DSD-FME and listed as "
               "they appear (voice or control channels, systems, talkgroups, radios), and you can listen to all of "
               "them at once or single out a channel, talkgroup or radio.")
    discover = True
    explorer = True
    settings = tuple(replace(s, default=DEFAULTS[s.key]) if s.key in DEFAULTS else s
                     for s in ReceiverMode.settings) + (
        Setting("discover_snr", "Find signals stronger than", 12, "int", 6, 40, 1, suffix=" dB over the noise",
                help="Signals this far above the band's noise get a decoder to try them. Lower finds weaker ones "
                     "but tries more noise."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR; slide along the band (the grab bar, ◀ ▶) to where digital radios are",
        bandwidth="12.5 kHz channels (6.25 kHz for NXDN48 and dPMR); up to 24 decoded at once",
        frequencies=(("438-440 MHz", "amateur DMR and P25 repeaters"),
                     ("400-520 MHz", "business, transport and government DMR, P25 and NXDN"),
                     ("380-400, 800-870 MHz", "public-safety P25 in some countries (often encrypted)")),
        notes="Each signal that shows up is tried for about 20 s; digital voice stays in the lists, anything else is "
              "left alone for 10 minutes. Repeaters only transmit during calls, so leave it on a busy part of the "
              "band for a while. Encrypted calls are listed but can't be heard. Listening to some services is "
              "restricted by law in some countries.",
    )
