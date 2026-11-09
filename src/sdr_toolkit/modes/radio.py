"""Modes that need raw radio samples: the RTL-SDR, or an IQ recording of one.

These don't use the speaker capture. Each runs a decoder that opens the
RTL-SDR itself (rtl_433, readsb, rtl_fm), or reads a recording: unsigned
8-bit IQ, as ``rtl_sdr`` writes it (``.cu8``), made at the rate the mode's
"How to tune" gives.
"""

from pathlib import Path

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt

from .base import Event, Setting
from .process import ProcessMode

RADIO_SETTINGS = (
    Setting("input", "Input", "rtl", "choice", restart=True,
            choices=(("rtl", "RTL-SDR"), ("file", "IQ recording (.cu8)")),
            help="A recording is unsigned 8-bit IQ, as rtl_sdr writes it, at the sample rate below."),
    Setting("file", "Recording", "", "text", restart=True, help="Path of the .cu8 recording."),
    Setting("device", "Device", "0", "text", restart=True,
            help="RTL-SDR index (0 is the first) or :serial (e.g. :00000001)."),
    Setting("gain", "Gain", "auto", "text", restart=True,
            help="'auto', or dB (e.g. 40.2). Lower it if strong local signals overload the dongle."),
    Setting("ppm", "Frequency correction", 0, "int", -200, 200, restart=True, suffix=" ppm",
            help="The dongle's crystal error. Cheap dongles are often 30-80 ppm off; TCXO ones about 1."),
)


def read_cu8(path, start=0, count=None) -> np.ndarray:
    """Complex samples from an unsigned 8-bit IQ file."""
    raw = np.fromfile(path, dtype=np.uint8, offset=2 * start, count=-1 if count is None else 2 * count)
    raw = raw[: len(raw) // 2 * 2].astype(np.float32)
    return ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)) / 127.5


def write_cu8(path, iq: np.ndarray, scale=1.0):
    """Write complex samples (|x| up to about 1) as an unsigned 8-bit IQ file."""
    out = np.empty(2 * len(iq), np.float32)
    out[0::2] = iq.real * scale
    out[1::2] = iq.imag * scale
    np.clip(out * 127.5 + 127.5, 0, 255).astype(np.uint8).tofile(path)


def fm_audio_chunks(path, iq_rate, offset_hz=0.0, audio_rate=48000, channel_hz=12500, chunk_s=0.5, level=0.2):
    """Narrow FM demodulation of a recording, as 16-bit audio bytes (a flat discriminator, no de-emphasis).

    *level* is the audio for a deviation of half the channel width; 0.2 puts 2.5 kHz FM
    (APRS, AIS) about where direwolf asks for it.
    """
    n = int(iq_rate * chunk_s)
    sos = butter(6, channel_hz / 2, fs=iq_rate, output="sos")
    zi = np.zeros((sos.shape[0], 2), complex)
    g = np.gcd(int(iq_rate), int(audio_rate))
    up, down = int(audio_rate) // g, int(iq_rate) // g
    last = 1 + 0j
    t0 = 0
    skip = 0
    size = Path(path).stat().st_size // 2
    for start in range(0, size, n):
        x = read_cu8(path, start, n)
        if offset_hz:
            x = x * np.exp(-2j * np.pi * offset_hz * (t0 + np.arange(len(x))) / iq_rate)
        t0 += len(x)
        x, zi = sosfilt(sos, x, zi=zi)
        prev = np.concatenate([[last], x[:-1]])
        last = x[-1]
        disc = np.angle(x * np.conj(prev)) * iq_rate / (2 * np.pi) / (channel_hz / 2)
        if up == 1:
            # Whole-number ratio: keep every down-th sample, carrying the phase over, so
            # chunk edges leave no glitch (the channel filter has done the low-pass).
            audio = disc[skip::down]
            skip = (skip - len(disc)) % down
        else:
            audio = resample_poly(disc, up, down)
        yield (np.clip(audio * level, -1, 1) * 32767).astype("<i2").tobytes()
    # A second of silence: decoders (direwolf) drop the last packet if the input ends right after it.
    yield bytes(2 * int(audio_rate))


class RadioMode(ProcessMode):
    source = "radio"
    takes_audio = False
    iq_rate = 250000                 # recording sample rate this mode expects

    def file_path(self) -> str:
        p = str(Path(self.values["file"]).expanduser()) if self.values["file"] else ""
        if not p or not Path(p).is_file():
            raise RuntimeError("Choose a recording: set Input to RTL-SDR, or give the path of a .cu8 file "
                               "in Settings → Recording.")
        return p

    def live(self) -> bool:
        return self.values["input"] == "rtl"

    def device_index(self) -> str:
        """rtl_fm / rtl_433 style: an index, or :serial."""
        return self.values["device"].strip() or "0"

    NO_DEVICE = "No RTL-SDR found: plug it in, or set Settings → Input to an IQ recording."

    def device_problem(self, line: str) -> list[Event] | None:
        """A clear message for the usual live-input failures, or None."""
        low = line.lower()
        if "no supported devices" in low or "no matching devices" in low:
            msg = self.NO_DEVICE
        elif "usb_claim_interface" in low or "device or resource busy" in low:
            msg = "The RTL-SDR is in use by another program (SDR++, GQRX, a readsb service...). Close it first."
        else:
            return None
        if getattr(self, "_problem", None) == msg:
            return []
        self._problem = msg
        return [Event("line", msg, style="warn"), Event("status", msg)]

    def finished(self, code):
        if getattr(self, "_problem", None):
            return []          # the reason is already on screen
        return super().finished(code)

    def start(self, values):
        self._problem = None
        super().start(values)

    def upstream_error(self, line):
        return self.device_problem(line) or super().upstream_error(line)

    def gain_db(self) -> str | None:
        g = str(self.values["gain"]).strip().lower()
        return None if g in ("", "auto") else g
