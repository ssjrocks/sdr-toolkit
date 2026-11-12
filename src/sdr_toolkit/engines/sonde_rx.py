"""Radiosondes: finds weather balloons' sondes in the dongle's band and decodes each one.

Reads cu8 IQ (stdin, or a recording) at --rate around --centre. Every DETECT_EVERY s of samples the
band's averaged spectrum shows narrow carriers well above the noise (or the --freqs given are
used); each gets a 48 kHz channel cut out of the samples (receiver/dsp.Channelizer). A few seconds
of its FM audio go to rs1729's dft_detect, which says which sonde type it is and the signal's
polarity, and then that type's decoder (rs41mod, dfm09mod, m10m20mod...) reads the channel as
float32 IQ and writes a JSON line per good frame. Times are counted in samples, so a recording
read faster than real time behaves as it would live.

Writes JSON lines to stdout:
  {"event": "frame", "freq": Hz, ...the decoder's fields (id, type, lat, lon, alt, vel_v...)}
  {"event": "channel", "freq": Hz, "state": "detecting" | "decoding" | "lost" | "unknown", "type": "..."}
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

from .. import system
from ..receiver import dsp

CHANNEL_RATE = dsp.AUDIO_RATE        # 48 kHz: every sonde's channel
CHANNEL_WIDTH = 20000                # Hz: GFSK sondes are 5-15 kHz wide
DETECT_EVERY = 2.0                   # s between looks at the spectrum
DETECT_AUDIO = 3.0                   # s of a new channel's FM audio for dft_detect
MIN_SNR = 12.0                       # dB above the band's noise floor for a carrier to count
LOST_AFTER = 120.0                   # s without a frame: the sonde is gone (landed, out of range)
RETRY_AFTER = 30.0                   # s before a signal dft_detect couldn't place is tried again
MERGE = 15000                        # Hz: carriers closer than this are the same sonde
# dft_detect's names -> (decoder, its options). --json is added to all; -i when the polarity is negative.
DECODERS = {
    "RS41": ("rs41mod", ["--ptu2"]),
    "RS92": ("rs92mod", ["--ptu"]),
    "DFM9": ("dfm09mod", ["--ptu", "--ecc"]),
    "M10": ("m10m20mod", ["--ptu"]),
    "M20": ("m10m20mod", ["--ptu"]),
    "LMS6": ("lms6Xmod", ["--vit", "--ecc"]),
    "IMET5": ("imet54mod", []),
    "MEISEI": ("meisei100mod", []),
    "MRZ": ("mp3h1mod", []),
    "MTS01": ("mts01mod", []),
}


def program(name, bin_dir=None) -> str | None:
    """A decoder: the app's own build (setup/build.sh), else one on PATH."""
    for d in ([Path(bin_dir)] if bin_dir else []) + [system.data_dir() / "radiosonde" / "bin"]:
        p = d / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return shutil.which(name)


_say_lock = threading.Lock()


def say(**event):
    with _say_lock:                                          # decoders' readers write from their threads
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()


def find_carriers(power_db, rate, centre):
    return dsp.find_carriers(power_db, rate, centre, 2 * CHANNEL_WIDTH, MIN_SNR, MERGE)


class Channel:
    def __init__(self, freq, offset):
        self.freq, self.offset = freq, offset
        self.state = "detecting"
        self.audio: list = []
        self.audio_n = 0
        self.decoder = None
        self.type = ""
        self.last_frame = None            # sample time of the last frame decoded
        self.since = None                 # sample time decoding started
        self.retry_at = 0.0


class Engine:
    def __init__(self, rate, centre, freqs=(), bin_dir=None, out=say):
        self.rate, self.centre, self.bin_dir, self.out = int(rate), float(centre), bin_dir, out
        self.chz = dsp.Channelizer(self.rate)
        self.channels: dict[float, Channel] = {}
        self.fixed = [float(f) for f in freqs]
        self.t = 0.0                                          # seconds of samples so far
        self._spec = None
        self._spec_n = 0
        self._next_detect = DETECT_EVERY
        self._ignored: dict[float, float] = {}                # freq -> sample time it may be tried again
        self._lock = threading.Lock()
        self._readers: list[threading.Thread] = []
        self._tmp = Path(tempfile.mkdtemp(prefix="sdr-toolkit-sondes-"))
        for f in self.fixed:
            self._add(f)

    # ---- channels ---------------------------------------------------------------------

    def _add(self, freq):
        off = freq - self.centre
        if abs(off) + CHANNEL_WIDTH / 2 > self.rate / 2:
            return
        ch = Channel(freq, off)
        self.channels[freq] = ch
        self.chz.set(freq, off, CHANNEL_WIDTH)
        self.out(event="channel", freq=freq, state="detecting", type="")

    def _drop(self, ch, state):
        if ch.decoder is not None:
            try:
                ch.decoder.stdin.close()
            except OSError:
                pass
            ch.decoder.terminate()
            ch.decoder = None
        self.chz.remove(ch.freq)
        self.channels.pop(ch.freq, None)
        self.out(event="channel", freq=ch.freq, state=state, type=ch.type)
        if ch.freq not in self.fixed:
            self._ignored[ch.freq] = self.t + (RETRY_AFTER if state == "unknown" else 0.0)

    def _detect_type(self, ch):
        """dft_detect on the channel's FM audio: (type, inverted) or None."""
        exe = program("dft_detect", self.bin_dir)
        if exe is None:
            return None
        audio = np.concatenate(ch.audio)
        path = self._tmp / f"{int(ch.freq)}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(CHANNEL_RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32000).astype("<i2").tobytes())
        try:
            res = subprocess.run([exe, str(path)], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            path.unlink(missing_ok=True)
        for line in reversed(res.splitlines()):
            name, _, score = line.partition(":")
            name = name.strip()
            try:
                value = float(score)
            except ValueError:
                continue
            if name in DECODERS and abs(value) >= 0.5:
                return name, value < 0
        return None

    def _start_decoder(self, ch, kind, inverted):
        name, opts = DECODERS[kind]
        exe = program(name, self.bin_dir)
        if exe is None:
            self.out(event="channel", freq=ch.freq, state="unknown", type=kind, error=f"{name} isn't installed")
            return False
        cmd = [exe, "--json", *opts] + (["-i"] if inverted else []) + \
              ["--IQ", "0.0", "--lpIQ", "-", str(CHANNEL_RATE), "32"]
        ch.decoder = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, bufsize=0)
        ch.type, ch.state, ch.since = kind, "decoding", self.t
        reader = threading.Thread(target=self._read, args=(ch, ch.decoder), daemon=True)
        reader.start()
        self._readers.append(reader)
        self.out(event="channel", freq=ch.freq, state="decoding", type=kind)
        return True

    def _read(self, ch, proc):
        for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("{"):
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                ch.last_frame = self.t
                self.out(event="frame", freq=ch.freq, **frame)

    # ---- samples ----------------------------------------------------------------------

    def feed(self, x: np.ndarray):
        """cu8-converted complex samples (any length)."""
        self.t += len(x) / self.rate
        self._spectrum(x)
        for freq, y in self.chz.process(x).items():
            ch = self.channels.get(freq)
            if ch is None or not len(y):
                continue
            if ch.state == "detecting":
                ch.audio.append(dsp.fm_discriminator(self.chz.channels[freq], y, 4800.0))
                ch.audio_n += len(y)
                if ch.audio_n >= DETECT_AUDIO * CHANNEL_RATE:
                    found = self._detect_type(ch)
                    ch.audio, ch.audio_n = [], 0
                    if found is None or not self._start_decoder(ch, *found):
                        self._drop(ch, "unknown")
            elif ch.decoder is not None:
                buf = np.empty(2 * len(y), np.float32)
                buf[0::2], buf[1::2] = y.real, y.imag
                try:
                    ch.decoder.stdin.write(buf.tobytes())
                except (BrokenPipeError, OSError, ValueError):
                    self._drop(ch, "lost")
                    continue
        with self._lock:
            for ch in list(self.channels.values()):
                if ch.state == "decoding":
                    last = ch.last_frame if ch.last_frame is not None else ch.since
                    if self.t - last > LOST_AFTER:
                        self._drop(ch, "lost")
        if self.t >= self._next_detect:
            self._next_detect = self.t + DETECT_EVERY
            self._look()

    def _spectrum(self, x, n=4096):
        m = len(x) // n
        if not m:
            return
        seg = x[: m * n].reshape(m, n) * np.hanning(n).astype(np.float32)
        p = np.sum(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0)
        self._spec = p if self._spec is None else self._spec + p
        self._spec_n += m

    def _look(self):
        if self._spec is None or self.fixed:
            self._spec, self._spec_n = None, 0
            return
        db = 10 * np.log10(np.fft.fftshift(self._spec / self._spec_n) + 1e-20)
        self._spec, self._spec_n = None, 0
        for f in find_carriers(db, self.rate, self.centre):
            if any(abs(f - g) < MERGE for g in self.channels):
                continue
            if any(abs(f - g) < MERGE and self.t < until for g, until in self._ignored.items()):
                continue
            self._add(round(f / 500) * 500)                  # sondes sit on whole kHz; 500 Hz is close enough

    def close(self):
        for ch in list(self.channels.values()):
            if ch.decoder is not None:
                try:
                    ch.decoder.stdin.close()
                except OSError:
                    pass
        for ch in list(self.channels.values()):
            if ch.decoder is not None:
                try:
                    ch.decoder.wait(5)
                except subprocess.TimeoutExpired:
                    ch.decoder.terminate()
        for reader in self._readers:                         # let the decoders' last lines out
            reader.join(2)
        shutil.rmtree(self._tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", default="-", help="cu8 file, or - for stdin")
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--centre", type=float, required=True, help="Hz")
    ap.add_argument("--freqs", default="", help="comma-separated Hz to decode (else found by itself)")
    ap.add_argument("--bin-dir", default=None)
    a = ap.parse_args(argv)
    freqs = [float(f) for f in a.freqs.split(",") if f.strip()]
    eng = Engine(a.rate, a.centre, freqs, a.bin_dir)
    src = sys.stdin.buffer if a.input == "-" else open(a.input, "rb")
    block = 2 * (a.rate // 10)                               # 0.1 s
    try:
        while True:
            data = src.read(block)
            if not data:
                break
            eng.feed(dsp.cu8(data))
    except KeyboardInterrupt:
        pass
    finally:
        eng.close()


if __name__ == "__main__":
    main()
