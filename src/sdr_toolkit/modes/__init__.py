"""All the modes, in the order the window lists them."""

from .adsb import ADSBMode
from .ais import AISMode
from .aprs import APRSMode
from .digital_voice import DigitalVoiceMode
from .explorer import ExplorerMode
from .gps_fix import GPSFixMode
from .receiver import ReceiverMode
from .sat_telemetry import SatTelemetryMode
from .satellites import SatelliteMode
from .sondes import SondeMode
from .morse import MorseMode
from .ism import ISMMode
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
    ReceiverMode, ADSBMode, AISMode, SondeMode, ISMMode, LoRaMode, LoRaFinderMode, ExplorerMode, DigitalVoiceMode,
    GPSFixMode,
    SatelliteMode, SatTelemetryMode,
]
GROUPS = ["Text", "Digital (weak signal)", "Pictures", "Packet & paging", "Tones", "RTL-SDR", "Satellites"]
