"""All the modes, in the order the window lists them."""

from .adsb import ADSBMode
from .ais import AISMode
from .aprs import APRSMode
from .digital_voice import DigitalVoiceMode
from .explorer import ExplorerMode
from .gps_fix import GPSFixMode
from .receiver import ReceiverMode
from .fm_stations import FMStationsMode
from .dab import DABMode
from .sat_telemetry import SatTelemetryMode
from .satellites import SatelliteMode
from .satdump import SatDumpMode
from .astro import HydrogenLineMode, MeteorScatterMode
from .ble import BLEMode
from .wifi import WiFiMode
from .zigbee import ZigbeeMode
from .sondes import SondeMode
from .morse import MorseMode
from .ism import ISMMode
from .meters import MetersMode
from .aero import AeroMode
from .navaids import NavaidMode
from .dsc import DSCMode
from .beacons406 import Beacon406Mode
from .lora import LoRaMode
from .lora_find import LoRaFinderMode
from .pager import PagerMode
from .sstv import SSTVMode
from .text_modes import NavtexMode, PSKMode, RTTYMode
from .tones import TonesMode
from .wefax import WefaxMode
from .wsjt import FT4Mode, FT8Mode

MODES = [
    MorseMode, RTTYMode, PSKMode, NavtexMode,
    FT8Mode, FT4Mode,
    SSTVMode, WefaxMode,
    APRSMode, PagerMode,
    TonesMode,
    ReceiverMode, FMStationsMode, DABMode, ADSBMode, AeroMode, NavaidMode, AISMode, DSCMode, Beacon406Mode, SondeMode, ISMMode, MetersMode, LoRaMode, LoRaFinderMode, ExplorerMode,
    DigitalVoiceMode,
    GPSFixMode,
    SatelliteMode, SatTelemetryMode, SatDumpMode, HydrogenLineMode, MeteorScatterMode,
    WiFiMode, BLEMode, ZigbeeMode,
]
GROUPS = ["Text", "Digital (weak signal)", "Pictures", "Packet & paging", "Tones", "SDR", "Satellites",
          "Wi-Fi & Bluetooth"]
# The groups that decode audio (from a WebSDR, a radio's speaker...): under one WebSDR branch in the window.
AUDIO_GROUPS = ("Text", "Digital (weak signal)", "Pictures", "Packet & paging", "Tones")
