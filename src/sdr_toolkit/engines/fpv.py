"""Analog FPV video at 5.8 GHz (drones' and FPV cameras' PAL / NTSC, FM-modulated), as pictures.

    python -m sdr_toolkit.engines.fpv --rate 19200000 [--input FILE] [--every S]

The IQ (centred on the video carrier) is FM-demodulated and low-passed to the luminance (3 MHz). Line syncs are
the deepest parts of the signal, one every 64 us (PAL) or 63.56 us (NTSC); a field starts after its vertical
sync, a run of half-line pulses. Each line's picture part (from 10.5 us after its sync for 52 us) is sampled
across 640 pixels and the field's lines stacked into a greyscale picture (colour is ignored).

JSON lines out:
  {"event": "field", "w": 640, "h": H, "std": "PAL" | "NTSC", "gray": base64 bytes, "lines": n, "sync": dB}
"""

import argparse
import base64
import json
import sys

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from ..receiver import dsp

WIDTH = 640
STANDARDS = {"PAL": (64.0e-6, 312), "NTSC": (63.556e-6, 262)}      # line period, lines a field
ACTIVE_START, ACTIVE_LEN = 10.5e-6, 52.0e-6
CHANNELS = {  # MHz: the usual 5.8 GHz FPV bands
    "R": (5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917), "A": (5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725),
    "B": (5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866), "E": (5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945),
    "F": (5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880),
}


class Video:
    def __init__(self, rate):
        self.rate = rate
        self.sos = butter(4, 3.0e6, fs=rate, output="sos")
        self.zi = sosfilt_zi(self.sos) * 0
        self.last = 1 + 0j
        self.buf = np.zeros(0, np.float32)

    def feed(self, x) -> list[dict]:
        prev = np.concatenate([[self.last], x[:-1]])
        self.last = x[-1]
        v = np.angle(x * np.conj(prev)).astype(np.float32)
        v, self.zi = sosfilt(self.sos, v, zi=self.zi)
        self.buf = np.concatenate([self.buf, v.astype(np.float32)])
        out = []
        need = int(self.rate * 64e-6 * 330)
        while len(self.buf) >= 2 * need:
            f = self._field(self.buf[:2 * need])
            if f is None:
                self.buf = self.buf[need:]
                continue
            out.append(f["field"])
            self.buf = self.buf[f["used"]:]
        return out

    def _field(self, v):
        rate = self.rate
        # The sync level: the deepest 5% of the signal (FM video: sync is the lowest frequency, or the highest
        # when inverted; pick the polarity whose extreme is the narrow pulses).
        lo, hi = np.percentile(v, [2, 98])
        mid = (lo + hi) / 2
        if (v < lo + 0.15 * (hi - lo)).mean() > (v > hi - 0.15 * (hi - lo)).mean():
            v = -v
            lo, hi = -hi, -lo
        sync = v < lo + 0.2 * (hi - lo)
        edges = np.flatnonzero(sync[1:] & ~sync[:-1]) + 1               # falling into sync
        if len(edges) < 100:
            return None
        gaps = np.diff(edges) / rate
        best = None
        for std, (period, lines) in STANDARDS.items():
            n = np.sum(np.abs(gaps - period) < 0.2e-6)             # PAL and NTSC lines are only 0.44 us apart
            if best is None or n > best[0]:
                best = (n, std, period, lines)
        n, std, period, lines = best
        if n < 50:
            return None
        # The field starts after the vertical sync: a run of half-line gaps (equalising / broad pulses).
        half = np.abs(gaps - period / 2) < 1.5e-6
        start = None
        run = 0
        for i, h in enumerate(half):
            run = run + 1 if h else 0
            if run >= 8 and i + 1 < len(half) and not half[i + 1]:
                start = edges[i + 1]
                break
        if start is None:
            return None
        spl = period * rate
        a0, alen = int(ACTIVE_START * rate), int(ACTIVE_LEN * rate)
        rows = []
        pos = start
        line_edges = edges[edges >= start]
        k = 0
        for _ in range(lines):
            # Lock each line to the nearest sync edge when there's one close by.
            j = np.searchsorted(line_edges, pos - spl * 0.1)
            if j < len(line_edges) and abs(line_edges[j] - pos) < spl * 0.1:
                pos = line_edges[j]
            seg = v[int(pos) + a0:int(pos) + a0 + alen]
            if len(seg) < alen:
                break
            rows.append(np.interp(np.linspace(0, alen - 1, WIDTH), np.arange(alen), seg))
            pos += spl
            k += 1
        if len(rows) < lines * 0.8:
            return None
        img = np.array(rows)
        black = lo + 0.3 * (hi - lo)                                     # blanking just above sync
        img = np.clip((img - black) / (hi - black + 1e-9), 0, 1)
        gray = (img * 255).astype(np.uint8)
        sync_db = 20 * np.log10((hi - lo) / (np.std(v[sync]) + 1e-9))
        return {"used": int(pos), "field": {"event": "field", "w": WIDTH, "h": len(rows), "std": std,
                                            "gray": base64.b64encode(gray.tobytes()).decode(), "lines": len(rows),
                                            "sync": round(float(sync_db), 1)}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=19_200_000)
    ap.add_argument("--input")
    ap.add_argument("--every", type=int, default=5, help="send every n-th field")
    a = ap.parse_args(argv)
    vid = Video(a.rate)
    src = sys.stdin.buffer if not a.input else open(a.input, "rb")
    n = 0
    while True:
        data = src.read(1 << 21)
        if not data or len(data) < 2:
            break
        for f in vid.feed(dsp.cu8(data[:len(data) // 2 * 2])):
            n += 1
            if n % a.every == 1 or a.every == 1:
                sys.stdout.write(json.dumps(f) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    main()
