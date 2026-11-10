"""GPS L1 C/A position fixes with GNSS-SDR (gnss-sdr.org), and reading what it prints.

A dongle's crystal error moves both its tuning and its sample clock by the same
ppm: at 1575.42 MHz, 57 ppm puts the signals 90 kHz off, beyond the Doppler
search. GNSS-SDR's RTL-SDR example corrects this with the real sample rate
(``internal_fs_sps``) and the offset of the signal (``InputFilter.IF``); both
come from the app's frequency correction. A positive ppm (crystal fast, as
librtlsdr and readsb count it) puts the signal below the centre.
"""

import re
from dataclasses import dataclass, field

L1 = 1575.42e6
RATE = 2048000


def corrections(ppm: float, rate=RATE) -> tuple[int, int]:
    """(real sample rate, signal offset in Hz) for a dongle *ppm* fast."""
    return round(rate * (1 + ppm * 1e-6)), round(-L1 * ppm * 1e-6)


def config(source: dict, ppm: float, out_dir: str, channels=8, doppler=10000) -> str:
    """A GNSS-SDR configuration. *source* is ``{"live": device args, "gain": dB or None}`` or
    ``{"stdin": True}`` (signed 8-bit IQ on standard input)."""
    fs, offset = corrections(ppm)
    lines = [f"GNSS-SDR.internal_fs_sps={fs}"]
    if "live" in source:
        gain = source.get("gain")
        lines += [
            "SignalSource.implementation=Osmosdr_Signal_Source",
            f"SignalSource.osmosdr_args={source['live']}",
            "SignalSource.item_type=gr_complex",
            f"SignalSource.sampling_frequency={RATE}",
            f"SignalSource.freq={int(L1)}",
            f"SignalSource.AGC_enabled={'false' if gain is not None else 'true'}",
            f"SignalSource.gain={gain if gain is not None else 40}",
            f"SignalSource.rf_gain={gain if gain is not None else 40}",
            "SignalSource.if_gain=30",
            "DataTypeAdapter.implementation=Pass_Through",
        ]
    else:
        lines += [
            "SignalSource.implementation=Fifo_Signal_Source",
            "SignalSource.filename=/dev/stdin",
            "SignalSource.sample_type=ibyte",          # turned into complex samples by the source
            f"SignalSource.sampling_frequency={RATE}",
            "DataTypeAdapter.implementation=Pass_Through",
        ]
    lines += [
        "SignalSource.dump=false",
        "SignalConditioner.implementation=Signal_Conditioner",
        "InputFilter.implementation=Freq_Xlating_Fir_Filter",
        "InputFilter.input_item_type=gr_complex",
        "InputFilter.output_item_type=gr_complex",
        "InputFilter.taps_item_type=float",
        "InputFilter.number_of_taps=5",
        "InputFilter.number_of_bands=2",
        "InputFilter.band1_begin=0.0",
        "InputFilter.band1_end=0.45",
        "InputFilter.band2_begin=0.55",
        "InputFilter.band2_end=1.0",
        "InputFilter.ampl1_begin=1.0",
        "InputFilter.ampl1_end=1.0",
        "InputFilter.ampl2_begin=0.0",
        "InputFilter.ampl2_end=0.0",
        "InputFilter.band1_error=1.0",
        "InputFilter.band2_error=1.0",
        "InputFilter.filter_type=bandpass",
        "InputFilter.grid_density=16",
        f"InputFilter.sampling_frequency={fs}",
        f"InputFilter.IF={offset}",
        "Resampler.implementation=Pass_Through",
        f"Channels_1C.count={channels}",
        "Channels.in_acquisition=1",
        "Channel.signal=1C",
        "Acquisition_1C.implementation=GPS_L1_CA_PCPS_Acquisition",
        "Acquisition_1C.item_type=gr_complex",
        "Acquisition_1C.coherent_integration_time_ms=1",
        "Acquisition_1C.pfa=0.01",
        f"Acquisition_1C.doppler_max={doppler}",
        "Acquisition_1C.doppler_step=250",
        "Acquisition_1C.max_dwells=1",
        # A recording is read as fast as it's processed: acquisition must hold the samples up
        # while it searches, or it falls behind and stops finding satellites.
        f"Acquisition_1C.blocking={'false' if 'live' in source or source.get('realtime') else 'true'}",
        "Tracking_1C.implementation=GPS_L1_CA_DLL_PLL_Tracking",
        "Tracking_1C.item_type=gr_complex",
        "Tracking_1C.pll_bw_hz=35.0",
        "Tracking_1C.dll_bw_hz=2.0",
        "TelemetryDecoder_1C.implementation=GPS_L1_CA_Telemetry_Decoder",
        "Observables.implementation=Hybrid_Observables",
        "PVT.implementation=RTKLIB_PVT",
        "PVT.positioning_mode=Single",
        "PVT.iono_model=Broadcast",
        "PVT.trop_model=Saastamoinen",
        "PVT.output_rate_ms=1000",
        "PVT.display_rate_ms=1000",
        f"PVT.output_path={out_dir}",
        "PVT.flag_nmea_tty_port=false",
        "PVT.flag_rtcm_server=false",
        "PVT.flag_rtcm_tty_port=false",
    ]
    return "[GNSS-SDR]\n" + "\n".join(lines) + "\n"


@dataclass
class Satellite:
    prn: int
    channel: int
    cn0: float | None = None
    subframes: int = 0
    tracking: bool = True
    healthy: bool = True


@dataclass
class Receiver:
    """What GNSS-SDR's console output says: satellites tracked, and fixes."""
    sats: dict = field(default_factory=dict)       # prn -> Satellite
    fix: tuple | None = None                        # (lat, lon, height, used, utc text)
    fixes: int = 0

    TRACK = re.compile(r"Tracking of GPS L1 C/A signal started on channel (\d+) for satellite GPS PRN (\d+)")
    LOSS = re.compile(r"Loss of lock in channel (\d+)")
    NAV = re.compile(r"New GPS NAV message received in channel (\d+): subframe (\d+) from satellite "
                     r"GPS PRN (\d+).*?CN0=([\d.]+)")
    POS = re.compile(r"Position at (.+?) UTC using (\d+) observations is Lat = (-?[\d.]+) \[deg\], "
                     r"Long = (-?[\d.]+) \[deg\], Height ?= ?(-?[\d.]+) \[m\]")
    UNHEALTHY = re.compile(r"Satellite GPS PRN (\d+) .*?reports an unhealthy status")
    FIRST = re.compile(r"First position fix at (.+?) UTC is Lat = (-?[\d.]+) \[deg\], Long = (-?[\d.]+) "
                       r"\[deg\], Height ?= ?(-?[\d.]+) \[m\]")

    def feed(self, line: str) -> str | None:
        """Updates from one line; returns "track", "lost", "nav", "fix" or None."""
        if m := self.TRACK.search(line):
            ch, prn = int(m.group(1)), int(m.group(2))
            if not 1 <= prn <= 32:
                return None                        # two channels' lines printed into each other
            for s in list(self.sats.values()):
                if s.channel == ch:
                    s.tracking = False
            old = self.sats.get(prn)
            self.sats[prn] = Satellite(prn, ch, old.cn0 if old else None, old.subframes if old else 0)
            return "track"
        if m := self.LOSS.search(line):
            for s in self.sats.values():
                if s.channel == int(m.group(1)):
                    s.tracking = False
            return "lost"
        if m := self.UNHEALTHY.search(line):
            prn = int(m.group(1))
            if prn in self.sats:
                self.sats[prn].healthy = False
            return "unhealthy"
        if m := self.NAV.search(line):
            prn = int(m.group(3))
            if not 1 <= prn <= 32:
                return None
            s = self.sats.setdefault(prn, Satellite(prn, int(m.group(1))))
            s.cn0 = float(m.group(4))
            s.subframes += 1
            s.tracking = True
            return "nav"
        if m := self.POS.search(line):
            self.fix = (float(m.group(3)), float(m.group(4)), float(m.group(5)), int(m.group(2)), m.group(1))
            self.fixes += 1
            return "fix"
        if m := self.FIRST.search(line):
            self.fix = (float(m.group(2)), float(m.group(3)), float(m.group(4)), None, m.group(1))
            self.fixes += 1
            return "fix"
        return None

    def tracking(self) -> list[Satellite]:
        return sorted((s for s in self.sats.values() if s.tracking), key=lambda s: s.prn)
