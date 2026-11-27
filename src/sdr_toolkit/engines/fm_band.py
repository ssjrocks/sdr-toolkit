"""Every FM broadcast station in the band, with its RDS: name, radiotext, programme type, traffic flags, clock.

    python -m sdr_toolkit.engines.fm_band --port RTL_TCP_PORT --rate R [--dwell S] [--lo MHz --hi MHz]
    python -m sdr_toolkit.engines.fm_band --input FILE --rate R --centre Hz

Live, it talks rtl_tcp (the relay bridge, or an rtl_tcp of this PC's dongle) and steps across the band a window
at a time (85% of the sample rate: 2 MHz on an RTL-SDR, 16 MHz on a HackRF at 19.2 MS/s). In each window the
100 kHz channels with a carrier MIN_SNR above the window's noise are cut out at 240 kHz by one FFT channelizer,
FM-demodulated, and their multiplex read by the RDS decoder (engines/rds.py) for --dwell seconds; what each
station has said is kept from one sweep to the next. A recording is one window.

Writes JSON lines: {"event": "window", "lo", "hi", "stations": [MHz...]} and, whenever a station's RDS changes
and at the end of each dwell, {"event": "station", "freq": MHz, "snr": dB, "pilot": bool, ...Station.summary()}.
"""

import argparse
import json
import socket
import struct
import sys
import time

import numpy as np

from ..receiver import dsp
from . import rds

CH_RATE = 240000
MIN_SNR = 8.0                      # dB; the RDS decoder ignores noise, so a weak carrier costs only CPU
SETTLE = 0.3                       # s skipped after a retune
MEASURE = 1.0                      # s of spectrum to find the stations
SET_FREQ, SET_RATE, SET_GAIN_MODE, SET_GAIN = 1, 2, 3, 4


def windows(lo, hi, rate):
    """Window centres covering lo..hi (Hz) with the middle 85% of each."""
    use = rate * 0.85
    n = max(1, int(np.ceil((hi - lo) / use)))
    step = (hi - lo) / n
    # Halfway between two 100 kHz channels, so no station sits on the SDR's DC spike.
    return [round((lo + (i + 0.5) * step - 50e3) / 100e3) * 100e3 + 50e3 for i in range(n)]


def stations_in(x, rate, centre, lo=87.5e6, hi=108.0e6):
    """The 100 kHz channels with a carrier MIN_SNR above the window's noise: [(MHz, snr)]."""
    n = 1 << 14
    segs = [x[i:i + n] for i in range(0, len(x) - n, n)][:64]
    if not segs:
        return []
    w = np.hanning(n)
    spec = np.mean([np.abs(np.fft.fftshift(np.fft.fft(s * w))) ** 2 for s in segs], axis=0)
    f = (np.arange(n) - n // 2) * rate / n + centre
    floor = np.median(spec) + 1e-30
    out = []
    for k in range(int(np.ceil(lo / 1e5)), int(hi / 1e5) + 1):
        mhz = k / 10
        if abs(mhz * 1e6 - centre) > rate * 0.425:
            continue
        sel = np.abs(f - mhz * 1e6) < 60e3
        snr = 10 * np.log10(spec[sel].mean() / floor)
        if snr >= MIN_SNR:
            out.append((round(mhz, 1), round(float(snr), 1)))
    # A strong station spills into the channels next to it: keep local peaks only.
    keep = []
    for i, (m, s) in enumerate(out):
        nb = [s2 for m2, s2 in out if abs(m2 - m) < 0.15 and m2 != m]
        if all(s >= s2 for s2 in nb):
            keep.append((m, s))
    return keep


class Window:
    """One window's stations being decoded."""

    def __init__(self, rate, centre, found, book):
        self.rate, self.centre = rate, centre
        self.chz = dsp.Channelizer(rate, m=256 if rate > 4e6 else 512, out_rate=CH_RATE)
        self.found = dict(found)
        self.dec = {}
        self.last = {}
        for mhz, _ in found:
            self.chz.set(mhz, mhz * 1e6 - centre, 200000)
            d = rds.Decoder(CH_RATE)
            d.station = book.setdefault(mhz, rds.Station())      # what was heard on earlier sweeps
            self.dec[mhz] = d
            self.last[mhz] = 1 + 0j
        self.pilot = {m: 0.0 for m in self.dec}

    def feed(self, x):
        changed = []
        for mhz, y in self.chz.process(x).items():
            if not len(y):
                continue
            prev = np.concatenate([[self.last[mhz]], y[:-1]])
            self.last[mhz] = y[-1]
            disc = np.angle(y * np.conj(prev))
            # The 19 kHz stereo pilot: its level against the multiplex around it.
            n = len(disc)
            t = np.arange(n) / CH_RATE
            p19 = abs(np.mean(disc * np.exp(-2j * np.pi * 19000 * t)))
            p17 = abs(np.mean(disc * np.exp(-2j * np.pi * 17200 * t))) + 1e-12
            self.pilot[mhz] = 0.9 * self.pilot[mhz] + 0.1 * (p19 / p17)
            if self.dec[mhz].feed(disc):
                changed.append(mhz)
        return changed

    def report(self, mhz):
        s = self.dec[mhz].station.summary()
        return dict(s, event="station", freq=mhz, snr=self.found.get(mhz), pilot=bool(self.pilot[mhz] > 4))


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def cmd(c, v):
    return struct.pack(">BI", c, int(v) & 0xFFFFFFFF)


def run_live(a):
    try:
        sock = socket.create_connection(("127.0.0.1", a.port), timeout=10)
    except OSError as e:
        sys.exit(f"fm_band: couldn't reach the SDR's rtl_tcp: {e}")
    head = b""
    while len(head) < 12:
        d = sock.recv(12 - len(head))
        if not d:
            sys.exit("fm_band: the SDR's rtl_tcp closed the connection")
        head += d
    if a.gain:
        sock.sendall(cmd(SET_GAIN_MODE, 1) + cmd(SET_GAIN, int(float(a.gain) * 10)))
    sock.settimeout(10)
    book: dict[float, rds.Station] = {}
    centres = windows(a.lo * 1e6, a.hi * 1e6, a.rate)
    i = 0
    while True:
        centre = centres[i % len(centres)]
        sock.sendall(cmd(SET_RATE, a.rate) + cmd(SET_FREQ, int(centre)))
        need = int((SETTLE + MEASURE) * a.rate) * 2
        buf = bytearray()
        try:
            while len(buf) < need:
                d = sock.recv(1 << 20)
                if not d:
                    sys.exit("fm_band: the SDR's rtl_tcp closed the connection")
                buf += d
        except TimeoutError:
            sys.exit("fm_band: no samples for 10 s: the SDR isn't streaming")
        x = dsp.cu8(bytes(buf[int(SETTLE * a.rate) * 2:need]))
        found = stations_in(x, a.rate, centre, a.lo * 1e6, a.hi * 1e6)
        lo, hi = centre - a.rate * 0.425, centre + a.rate * 0.425
        emit({"event": "window", "lo": round(lo / 1e6, 2), "hi": round(hi / 1e6, 2),
              "stations": [m for m, _ in found]})
        if found:
            t_dwell = time.monotonic()
            w = Window(a.rate, centre, found, book)
            w.feed(x)
            got = 0
            pend = b""
            until = a.dwell * a.rate
            while got < until:
                try:
                    d = sock.recv(1 << 20)
                except TimeoutError:
                    sys.exit("fm_band: no samples for 10 s: the SDR isn't streaming")
                if not d:
                    sys.exit("fm_band: the SDR's rtl_tcp closed the connection")
                d = pend + d
                cut = len(d) // 2 * 2
                pend = d[cut:]
                got += cut // 2
                for mhz in w.feed(dsp.cu8(d[:cut])):
                    emit(w.report(mhz))
            for mhz in w.dec:
                emit(w.report(mhz))
            # How it kept up: samples received against the time they took (below 1: the SDR's stream is
            # being held back, and the relay drops what doesn't fit).
            emit({"event": "speed", "samples_s": round(got / a.rate, 2), "wall_s": round(time.monotonic() - t_dwell, 2)})
        i += 1
        if a.sweeps and i >= a.sweeps * len(centres):
            return


def run_file(a):
    raw = np.memmap(a.input, np.uint8, mode="r")
    first = dsp.cu8(bytes(raw[:int(MEASURE * a.rate) * 2]))
    found = stations_in(first, a.rate, a.centre)
    emit({"event": "window", "lo": round((a.centre - a.rate * 0.425) / 1e6, 2),
          "hi": round((a.centre + a.rate * 0.425) / 1e6, 2), "stations": [m for m, _ in found]})
    if not found:
        return
    w = Window(a.rate, a.centre, found, {})
    step = a.rate // 10 * 2
    for i in range(0, len(raw) // 2 * 2, step):
        for mhz in w.feed(dsp.cu8(bytes(raw[i:i + step]))):
            emit(w.report(mhz))
    for mhz in w.dec:
        emit(w.report(mhz))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int)
    ap.add_argument("--input")
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--centre", type=float)
    ap.add_argument("--dwell", type=float, default=20.0)
    ap.add_argument("--lo", type=float, default=87.5)
    ap.add_argument("--hi", type=float, default=108.0)
    ap.add_argument("--gain", default="")
    ap.add_argument("--sweeps", type=int, default=0, help="stop after this many sweeps (0: never)")
    a = ap.parse_args(argv)
    if a.rate % CH_RATE:
        sys.exit(f"fm_band: the sample rate must be a multiple of {CH_RATE}")
    if a.input:
        run_file(a)
    else:
        run_live(a)


if __name__ == "__main__":
    t0 = time.time()
    main()
