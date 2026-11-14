"""LoRa finder: finds the bursts in the dongle's band and works out what each one is, without being
told any settings.

Reads cu8 IQ (stdin, or a recording) at --rate around --centre. The band's spectrum, averaged over
about 14 ms at a time, shows bursts as bins well above its noise; each burst is followed until it
stops. Then:

1. Its width, from its average spectrum (bins within 15 dB of its peak). A LoRa chirp sweeps its
   whole bandwidth evenly, so LoRa is 62.5, 125, 250 or 500 kHz wide and flat; anything else is
   said to be something else (narrow FSK, a carrier...).
2. Its spreading factor: the burst is brought down to one sample per chip (its bandwidth) and cut
   into symbol-long pieces; multiplied by the base down-chirp, each LoRa symbol becomes one tone,
   so one FFT bin holds most of the piece's energy, but only for the right spreading factor (7-12).
   Tried both ways round: LoRaWAN's downlinks send their chirps inverted.
3. Its sync word, and its bytes: gr-lora_sdr (engines/lora_rx.py) decodes the burst with those
   settings and each sync word in turn (0x34 LoRaWAN, 0x2B Meshtastic, 0x12 private LoRa and
   MeshCore); it finds the frame only with the right one.

Writes JSON lines to stdout, one per burst:
  {"event": "burst", "freq": Hz, "width": Hz, "dur": s, "lora": bool, "sf", "bw", "inverted",
   "score", "sync", "frames": [{"hex", "crc"}], "note"}
"""

import argparse
import collections
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading

import numpy as np

from .. import system
from ..receiver import dsp

BWS = (62500, 125000, 250000, 500000)
SFS = range(7, 13)
SYNCS = ("0x34", "0x2B", "0x12")    # LoRaWAN, Meshtastic, private LoRa / MeshCore
FFT = 2048
AVG = 16                             # FFT frames per look (about 14 ms at 2.4 MS/s)
SNR_DB = 10.0                        # a bin this far over the band's noise is part of a burst
HANG = 0.25                          # s a burst may be silent and still be going on
MAX_BURST = 6.0                      # s: a burst longer than this is cut here (a carrier, say)
MIN_BURST = 0.004                    # s: shorter is a click
MARGIN = 0.03                        # s of samples kept either side of a burst
RING = 9.0                           # s of samples kept for bursts to be cut out of
JOIN = 300000                        # Hz: a signal this close to a burst's edges is the same burst
CHIRPY = 0.3                         # share of a symbol's energy in one tone: LoRa (noise and FSK: far less)
QUEUE = 8                            # bursts waiting to be worked out; more are counted, not looked at


def say(**event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def base_chirp(sf) -> np.ndarray:
    n = np.arange(2 ** sf)
    return np.exp(1j * np.pi * (n * n / 2 ** sf - n)).astype(np.complex64)


def chirpiness(y, sf) -> float:
    """How LoRa-like y (one sample per chip) is at sf: per symbol-long piece, the share of its energy
    in its strongest tone once de-chirped; the 75th percentile over the pieces (some are silence)."""
    n = 2 ** sf
    k = len(y) // n
    if k < 3:
        return 0.0
    down = np.conj(base_chirp(sf))
    p = np.abs(np.fft.fft(y[: k * n].reshape(k, n) * down, axis=1)) ** 2
    # A piece across a symbol boundary splits its energy over two tones: count the best pair.
    top2 = np.sort(p, axis=1)[:, -2:].sum(axis=1)
    share = top2 / np.maximum(p.sum(axis=1), 1e-20)
    return float(np.percentile(share, 75))


def lora_bw(width) -> int | None:
    """The LoRa bandwidth a burst this wide (Hz) can be, or None."""
    fits = [b for b in BWS if 0.7 * b <= width <= 1.45 * b]
    return min(fits, key=lambda b: abs(width - b)) if fits else None


class Finder:
    def __init__(self, rate, centre, snr_db=SNR_DB, out=say, decode=True):
        self.rate, self.centre, self.out = int(rate), float(centre), out
        self.thresh = 10 ** (snr_db / 10)
        self.decode = decode
        self.bin_hz = self.rate / FFT
        self.window = np.hanning(FFT).astype(np.float32)
        self.freqs = (np.arange(FFT) - FFT / 2) * self.bin_hz         # offsets from the centre
        usable = (np.abs(self.freqs) > 3 * self.bin_hz) & (np.abs(self.freqs) < 0.45 * self.rate)
        self.usable = usable
        self._ring: collections.deque = collections.deque()          # (first sample index, cu8 bytes)
        self._ring_n = 0
        self._n = 0                                                  # samples so far
        self._pending = np.zeros(0, np.complex64)
        self._frames: list = []
        self.bursts: list[dict] = []
        self.dropped = 0
        self._last_sync: dict = {}                                   # (freq / 25 kHz, bw, sf, inv) -> sync
        self._work: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._work_loop, daemon=True)
        self._worker.start()

    # ---- samples in ------------------------------------------------------------------------

    def feed(self, data: bytes):
        x = dsp.cu8(data)
        self._ring.append((self._n, data))
        self._ring_n += len(x)
        while self._ring_n - len(self._ring[0][1]) // 2 > RING * self.rate:
            self._ring_n -= len(self._ring.popleft()[1]) // 2
        start = self._n - len(self._pending)
        y = np.concatenate([self._pending, x]) if len(self._pending) else x
        self._n += len(x)
        k = len(y) // FFT
        if k:
            seg = y[: k * FFT].reshape(k, FFT) * self.window
            p = np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2
            for i in range(k):
                self._frames.append(p[i])
                if len(self._frames) == AVG:
                    self._look(np.mean(self._frames, axis=0), start + (i + 1) * FFT)
                    self._frames = []
        self._pending = y[k * FFT:]

    def _look(self, p, now):
        """One averaged spectrum ending at sample `now`: follow the bursts in it."""
        floor = float(np.median(p[self.usable]))
        hot = (p > floor * self.thresh) & self.usable
        regions = []
        i = 0
        while i < FFT:
            if not hot[i]:
                i += 1
                continue
            j = i
            gap = 0
            while j + 1 < FFT and gap <= 2:
                j += 1
                gap = 0 if hot[j] else gap + 1
            j -= gap
            if j - i >= 2:
                regions.append((self.freqs[i], self.freqs[j]))
            i = j + 1
        for lo, hi in regions:
            b = next((b for b in self.bursts if lo < b["hi"] + JOIN and hi > b["lo"] - JOIN
                      and max(hi, b["hi"]) - min(lo, b["lo"]) < 2 * JOIN), None)
            if b is None:
                b = {"lo": lo, "hi": hi, "t0": now - AVG * FFT, "t1": now, "spec": np.zeros(FFT), "looks": 0}
                self.bursts.append(b)
            b["lo"], b["hi"], b["t1"] = min(lo, b["lo"]), max(hi, b["hi"]), now
        for b in self.bursts:
            if b["t1"] == now:
                b["spec"] += p
                b["looks"] += 1
        for b in list(self.bursts):
            if (now - b["t1"]) / self.rate > HANG or (b["t1"] - b["t0"]) / self.rate > MAX_BURST:
                self.bursts.remove(b)
                self._ended(b, floor)

    def _ended(self, b, floor):
        dur = (b["t1"] - b["t0"]) / self.rate
        if dur < MIN_BURST:
            return
        spec = b["spec"] / max(b["looks"], 1)
        inside = (self.freqs >= b["lo"] - 50000) & (self.freqs <= b["hi"] + 50000)
        peak = float(spec[inside].max())
        occ = np.flatnonzero(inside & (spec >= peak / 10 ** 1.5))
        lo, hi = self.freqs[occ[0]] - self.bin_hz / 2, self.freqs[occ[-1]] + self.bin_hz / 2
        width, off = hi - lo, (lo + hi) / 2
        seg = self._cut(b["t0"] - int(MARGIN * self.rate), b["t1"] + int(MARGIN * self.rate))
        info = {"freq": self.centre + off, "offset": off, "width": width, "dur": dur,
                "snr_db": 10 * math.log10(max(peak / floor, 1e-9))}
        if self._work.qsize() >= QUEUE:
            self.dropped += 1
            self.out(event="busy", dropped=self.dropped)
            return
        self._work.put((info, seg))

    def _cut(self, a, z) -> bytes:
        """cu8 bytes of samples a..z (as many as are still kept)."""
        parts = []
        for first, data in self._ring:
            n = len(data) // 2
            lo, hi = max(a, first), min(z, first + n)
            if lo < hi:
                parts.append(data[2 * (lo - first): 2 * (hi - first)])
        return b"".join(parts)

    # ---- one burst ---------------------------------------------------------------------------

    def _work_loop(self):
        while True:
            item = self._work.get()
            if item is None:
                return
            try:
                self.out(event="burst", **self.examine(*item))
            except Exception as e:  # one odd burst mustn't stop the finder
                self.out(event="error", text=f"{type(e).__name__}: {e}")
            finally:
                self._work.task_done()

    def examine(self, info, seg) -> dict:
        r = dict(info, lora=False, sf=None, bw=None, inverted=False, score=0.0, sync=None, frames=[], note="")
        bw = lora_bw(info["width"])
        if bw is None:
            r["note"] = ("narrow: not LoRa (FSK, OOK or a carrier)" if info["width"] < 0.7 * BWS[0] else
                         "not a LoRa width")
            return r
        x = dsp.cu8(seg)
        t = np.arange(len(x)) / self.rate
        x = (x * np.exp(-2j * np.pi * info["offset"] * t)).astype(np.complex64)
        from scipy.signal import resample_poly
        g = math.gcd(bw, self.rate)
        y = resample_poly(x, bw // g, self.rate // g).astype(np.complex64)
        best = (0.0, None, False)
        for inv in (False, True):
            z = np.conj(y) if inv else y
            for sf in SFS:
                s = chirpiness(z, sf)
                if s > best[0]:
                    best = (s, sf, inv)
        score, sf, inv = best
        r.update(bw=bw, score=round(score, 3))
        if sf is None or score < CHIRPY:
            r["note"] = f"{bw / 1000:g} kHz wide but no LoRa chirps in it"
            return r
        r.update(lora=True, sf=sf, inverted=inv)
        if self.decode:
            self._decode(r, seg)
        return r

    def _decode(self, r, seg):
        """gr-lora_sdr on the burst with each sync word, the one that worked here before first."""
        key = (round(r["freq"] / 25000), r["bw"], r["sf"], r["inverted"])
        order = sorted(SYNCS, key=lambda s: s != self._last_sync.get(key))
        if r["inverted"]:
            b = bytearray(seg)
            b[1::2] = bytes(255 - v for v in b[1::2])         # conjugate: the chirps the right way round
            seg = bytes(b)
        with tempfile.NamedTemporaryFile(suffix=".cu8", delete=False) as f:
            f.write(seg)
            path = f.name
        try:
            for sync in order:
                frames = self._run_rx(path, r, sync)
                if frames:
                    r["sync"], r["frames"] = sync, frames
                    self._last_sync[key] = sync
                    return
            r["note"] = "LoRa, but no frame decoded (weak, cut short, or an unusual sync word)"
        finally:
            os.unlink(path)

    def _run_rx(self, path, r, sync) -> list:
        cmd = system.python_command("sdr_toolkit.engines.lora_rx") + [
            "--input", path, "--rate", str(self.rate), "--offset", str(r["offset"]), "--bw", str(r["bw"]),
            "--sf", str(r["sf"]), "--cr", "1", "--sync", sync, "--preamble", "8", "--centre", str(int(r["freq"]))]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return []
        frames, msg = [], None
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith("rx msg:"):
                try:
                    msg = bytes(int(v, 16) for v in line[7:].split(",") if v.strip())
                except ValueError:
                    msg = None
                if msg is not None and not r["inverted"]:
                    continue                                 # its CRC line follows
                if msg is not None:                          # a downlink: sent without a CRC
                    frames.append({"hex": msg.hex(), "crc": None})
                    msg = None
            elif line.startswith("CRC") and msg is not None:
                frames.append({"hex": msg.hex(), "crc": "valid" in line and "invalid" not in line})
                msg = None
        if msg is not None:
            frames.append({"hex": msg.hex(), "crc": None})
        return frames

    def close(self):
        if self.bursts:
            floor = 1.0
            for b in list(self.bursts):
                self.bursts.remove(b)
                self._ended(b, floor)
        self._work.join()
        self._work.put(None)
        self._worker.join(5)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", default="-", help="cu8 file, or - for stdin")
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--centre", type=float, required=True, help="Hz")
    ap.add_argument("--snr", type=float, default=SNR_DB, help="dB over the noise for a burst")
    ap.add_argument("--no-decode", action="store_true", help="only find and measure the bursts")
    a = ap.parse_args(argv)
    f = Finder(a.rate, a.centre, a.snr, decode=not a.no_decode)
    say(event="ready", rate=a.rate, centre=a.centre)
    src = sys.stdin.buffer if a.input == "-" else open(a.input, "rb")
    block = 2 * (a.rate // 20)                               # 50 ms
    try:
        while True:
            data = src.read(block)
            if not data:
                break
            f.feed(data)
    except KeyboardInterrupt:
        pass
    finally:
        f.close()
        say(event="done", dropped=f.dropped)


if __name__ == "__main__":
    main()
