"""All the modes, in the order the window lists them."""

from .adsb import ADSBMode
from .ais import AISMode
from .aprs import APRSMode
from .digital_voice import DigitalVoiceMode
from .gps_fix import GPSFixMode
from .receiver import ReceiverMode
from .satellites import SatelliteMode
from .morse import MorseMode
from .ism import ISMMode
from .lora import LoRaMode
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
    ReceiverMode, ADSBMode, AISMode, ISMMode, LoRaMode, DigitalVoiceMode, GPSFixMode,
    SatelliteMode,
]
GROUPS = ["Text", "Digital (weak signal)", "Pictures", "Packet & paging", "Tones", "RTL-SDR", "Satellites"]
