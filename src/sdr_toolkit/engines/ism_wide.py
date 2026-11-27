"""Whole ISM bands at once from a HackRF: rtl_433 on every channel of a band at the same time.

    python -m sdr_toolkit.engines.ism_wide --port BRIDGE_PORT --bands 'LO-HI,LO-HI,...' [--hop S]
        [--ppm N] -- RTL_433_ARGS...

It talks rtl_tcp to the window's relay bridge (a relay's HackRF, or this PC's through the built-in
relay): each band (edges in Hz) is sampled at the HackRF rate that covers it (2.4 to 19.2 MS/s),
cut into channels of 1.2 MS/s spaced at most 1 MHz apart (overlapping, so a device on a channel's
edge is in the next one too) by one FFT channelizer, and each channel goes to an rtl_433 of its own
reading unsigned 8-bit IQ from stdin. Their JSON lines come out on stdout, each with "freq" set
from its channel; rtl_433's other output (its pulse analyser with -A, its log) comes out on stdout
too, after a "[MHz] " tag. With several bands it hops between them every --hop seconds.
"""

import argparse
import json
import math
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np

SET_FREQ, SET_RATE, SET_GAIN_MODE, SET_GAIN, SET_PPM = 1, 2, 3, 4, 5
SET_FREQ_HI = 0x40
CHANNEL_RATE = 1_200_000
RTL433_CENTRE = 433.92                # MHz: what rtl_433 takes a recording's centre to be
HACKRF_RATES = (2_400_000, 4_800_000, 9_600_000, 19_200_000)
QUIET = 10.0
_print = threading.Lock()


def command(cmd, value) -> bytes:
    return struct.pack(">BI", cmd, int(value) & 0xFFFFFFFF)


def emit(line: str):
    with _print:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def plan(lo: float, hi: float) -> tuple[int, float, list[float]]:
    """(sample rate, centre, channel centres) for a band: the smallest HackRF rate whose usable 90% covers it,
    and channels at most 1 MHz apart across it."""
    span = hi - lo
    rate = next((r for r in HACKRF_RATES if r * 0.9 >= span), HACKRF_RATES[-1])
    centre = (lo + hi) / 2
    n = max(1, math.ceil(span / 1e6))
    return rate, centre, [lo + (i + 0.5) * span / n for i in range(n)]


def split(bands: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Bands wider than a HackRF sees at once (17 MHz of its 19.2) as several."""
    out = []
    for lo, hi in bands:
        n = max(1, math.ceil((hi - lo) / 17e6))
        step = (hi - lo) / n
        out += [(lo + i * step, lo + (i + 1) * step) for i in range(n)]
    return out


class Band:
    """One band's rtl_433s and the channelizer feeding them."""

    def __init__(self, lo, hi, rtl433_args):
        from ..receiver import dsp
        self.rate, self.centre, self.freqs = plan(lo, hi)
        self.chz = dsp.Channelizer(self.rate, m=1024, out_rate=CHANNEL_RATE)
        for f in self.freqs:
            self.chz.set(f, f - self.centre, CHANNEL_RATE * 0.85)
        self.procs = {}
        for f in self.freqs:
            # No -f: with a file or pipe it decodes nothing. Its frequencies are then 433.92 MHz plus the
            # offset it measured, rebased onto the channel below.
            p = subprocess.Popen(["rtl_433", "-r", "cu8:-", "-s", str(CHANNEL_RATE), *rtl433_args],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
            self.procs[f] = p
            threading.Thread(target=self._read, args=(f, p), daemon=True).start()

    @staticmethod
    def _read(f, p):
        for raw in p.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                for k in ("freq", "freq1", "freq2"):
                    if isinstance(d.get(k), (int, float)):
                        d[k] = round(f / 1e6 + float(d[k]) - RTL433_CENTRE, 4)
                d.setdefault("freq", round(f / 1e6, 4))
                emit(json.dumps(d))
            else:
                emit(f"[{f / 1e6:.3f}] {line}")

    def feed(self, x):
        for f, y in self.chz.process(x).items():
            if not len(y):
                continue
            b = np.empty(2 * len(y), np.uint8)
            b[0::2] = np.clip(np.rint(y.real * 127.5 + 127.5), 0, 255)
            b[1::2] = np.clip(np.rint(y.imag * 127.5 + 127.5), 0, 255)
            try:
                self.procs[f].stdin.write(b.tobytes())
            except (BrokenPipeError, OSError):
                pass

    def close(self):
        for p in self.procs.values():
            try:
                p.stdin.close()
            except OSError:
                pass
        for p in self.procs.values():
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.terminate()


def tune(sock, rate, freq):
    f = int(round(freq))
    sock.sendall(command(SET_RATE, rate) + (command(SET_FREQ_HI, f >> 32) if f >> 32 else b"")
                 + command(SET_FREQ, f & 0xFFFFFFFF))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--bands", required=True, help="LO-HI,LO-HI in Hz")
    ap.add_argument("--hop", type=float, default=60)
    ap.add_argument("--ppm", type=int, default=0)
    a, rtl433_args = ap.parse_known_args(argv)
    from ..receiver import dsp
    rtl433_args = [x for x in rtl433_args if x != "--"]
    bands = split([tuple(float(v) for v in b.split("-")) for b in a.bands.split(",")])
    try:
        sock = socket.create_connection((a.host, a.port), timeout=10)
    except OSError as e:
        sys.exit(f"ism_wide: couldn't reach the relay bridge: {e}")
    head = b""
    while len(head) < 12:
        data = sock.recv(12 - len(head))
        if not data:
            sys.exit("ism_wide: the relay closed the connection (busy, wrong password or no SDR: see its window)")
        head += data
    sock.sendall(command(SET_PPM, a.ppm & 0xFFFFFFFF))
    sock.settimeout(QUIET)
    i = 0
    while True:
        lo, hi = bands[i % len(bands)]
        band = Band(lo, hi, rtl433_args)
        tune(sock, band.rate, band.centre)
        emit(json.dumps({"event": "band", "lo": lo, "hi": hi, "rate": band.rate, "centre": band.centre,
                         "channels": [round(f / 1e6, 3) for f in band.freqs]}))
        skip = int(0.3 * band.rate) * 2            # 0.3 s of samples: the HackRF restarting at the new rate
        until = time.monotonic() + a.hop if len(bands) > 1 else math.inf
        buf = b""
        try:
            while time.monotonic() < until:
                try:
                    data = sock.recv(1 << 20)
                except TimeoutError:
                    sys.exit(f"ism_wide: no samples for {QUIET:.0f} s: the SDR isn't streaming")
                if not data:
                    sys.exit("ism_wide: the relay closed the connection")
                if skip > 0:
                    cut = min(skip, len(data))
                    skip -= cut
                    data = data[cut:]
                buf += data
                n = len(buf) // 2 * 2
                if n >= 1 << 18:
                    band.feed(dsp.cu8(buf[:n]))
                    buf = buf[n:]
        finally:
            band.close()
        i += 1


if __name__ == "__main__":
    main()
