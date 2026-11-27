"""Radio astronomy on an SDR: the 21 cm hydrogen line of our galaxy, and meteors' reflections.

    python -m sdr_toolkit.engines.astro hline --rate R [--input FILE] [--every S]      (IQ centred on 1420.406 MHz)
    python -m sdr_toolkit.engines.astro meteors --rate R --offset HZ [--input FILE] [--threshold DB]

hline: averages the power spectrum, takes away the receiver's own shape (a polynomial fitted to the edges,
outside +-150 km/s of the line) and gives, every --every seconds, the excess against radial velocity
(v = c (f0 - f) / f0, positive away from us; not corrected for the Earth's or Sun's motion).

meteors: watches a distant transmitter's carrier (--offset from the centre) that is normally out of reach: a
meteor's ionised trail reflects it for a fraction of a second to a few seconds. Each "ping" above --threshold dB
over the noise is written with its length, strength and Doppler.

Reads cu8 on stdin or from --input. JSON lines out:
  {"event": "spectrum", "seconds", "v": [...km/s], "excess": [...%], "peak_v", "peak"}
  {"event": "ping", "t" (s into the stream), "duration", "snr", "doppler", "wall"}
"""

import argparse
import json
import sys
import time

import numpy as np

from ..receiver import dsp

H1 = 1420.405751e6
C_KM_S = 299792.458


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def chunks(a, size):
    src = sys.stdin.buffer if a.input in (None, "-") else open(a.input, "rb")
    while True:
        d = src.read(size)
        if not d or len(d) < 2:
            return
        yield dsp.cu8(d[:len(d) // 2 * 2])


class HLine:
    def __init__(self, rate, n=4096):
        self.rate, self.n = rate, n
        self.acc = np.zeros(n)
        self.count = 0
        self.w = np.hanning(n)
        self.f = (np.arange(n) - n // 2) * rate / n          # offset from the line (the centre)
        self.v = -self.f / H1 * C_KM_S

    def feed(self, x):
        m = len(x) // self.n
        if not m:
            return
        blocks = x[:m * self.n].reshape(m, self.n) * self.w
        self.acc += (np.abs(np.fft.fftshift(np.fft.fft(blocks, axis=1), axes=1)) ** 2).sum(axis=0)
        self.count += m

    def result(self):
        p = self.acc / max(self.count, 1)
        c = self.n // 2
        p[c - 2:c + 3] = np.interp(np.arange(c - 2, c + 3), [c - 3, c + 3], [p[c - 3], p[c + 3]])   # the DC spike
        db = 10 * np.log10(p + 1e-30)
        # The flat middle of the receiver's band only: an SDR's filter rolls off faster near the edges than a
        # baseline can follow (a HackRF at 2.4 MS/s showed a +19% "line" at -200 km/s that was its filter).
        inband = np.abs(self.f) < self.rate * 0.35
        edge = (np.abs(self.v) > 150) & inband
        if edge.sum() < 20:
            edge = (np.abs(self.v) > 100) & inband
        # A straight line when the edges are narrow strips (there a curve's constant and curvature can't be told
        # apart, and noise picks the middle); a gentle curve when they're wide.
        span = float(np.abs(self.v[edge]).max() - np.abs(self.v[edge]).min()) if edge.any() else 0.0
        coef = np.polyfit(self.f[edge] / 1e6, db[edge], 2 if span > 100 else 1)
        base = np.polyval(coef, self.f / 1e6)
        excess = (10 ** ((db - base) / 10) - 1) * 100            # % above the receiver's shape
        # 5 km/s smoothing (hydrogen's features are tens of km/s wide), so a noisy bin isn't the peak.
        k = max(1, int(round(5.0 / (self.rate / self.n / H1 * C_KM_S))))
        # Narrow interference out first (one ~30 kHz wide at 1421.5 MHz here): a median over 20 km/s (95 kHz)
        # leaves hydrogen's features (20-50 km/s wide) and drops what's narrower than half that.
        from scipy.ndimage import median_filter
        excess = median_filter(excess, size=4 * k + 1, mode="nearest")
        sm = np.convolve(excess, np.ones(k) / k, mode="same")
        # The peak is looked for within +-180 km/s, where nearly all the galaxy's hydrogen is; interference outside
        # it (a 20% bump at -230 km/s, 1421.5 MHz, on this PC indoors) still shows on the plot.
        sel = inband & (np.abs(self.v) < 180)
        i = int(np.argmax(np.where(sel, sm, -1e9)))
        keep = inband & (np.abs(self.v) < 400)
        return {"v": [round(float(x), 2) for x in self.v[keep][::-1]],
                "excess": [round(float(x), 3) for x in sm[keep][::-1]],
                "peak_v": round(float(self.v[i]), 1), "peak": round(float(sm[i]), 3)}


def run_hline(a):
    h = HLine(a.rate)
    got, last = 0, 0
    for x in chunks(a, 1 << 20):
        h.feed(x)
        got += len(x)
        if got - last >= a.every * a.rate:
            last = got
            emit(dict(h.result(), event="spectrum", seconds=round(got / a.rate, 1)))
    if got > h.n:
        emit(dict(h.result(), event="spectrum", seconds=round(got / a.rate, 1)))


def run_meteors(a):
    n = 1024
    hz = a.rate / n
    w = np.hanning(n)
    search = int(np.ceil(a.search / hz))
    t = 0
    noise = None
    ping = None
    for x in chunks(a, n * 64 * 2):
        x = x * np.exp(-2j * np.pi * a.offset * (t + np.arange(len(x))) / a.rate)
        m = len(x) // n
        spec = np.abs(np.fft.fftshift(np.fft.fft(x[:m * n].reshape(m, n) * w, axis=1), axes=1)) ** 2
        c = n // 2
        for j in range(m):
            s = spec[j]
            band = s[c - search:c + search + 1]
            floor = np.median(np.concatenate([s[:c - 4 * search], s[c + 4 * search:]])) + 1e-30
            noise = floor if noise is None else 0.98 * noise + 0.02 * floor
            k = int(np.argmax(band))
            snr = 10 * np.log10(band[k] / noise)
            ft = (t + j * n) / a.rate
            if snr >= a.threshold:
                if ping is None:
                    ping = {"start": ft, "peak": snr, "doppler": (k - search) * hz, "last": ft, "frames": 0}
                ping["last"] = ft
                ping["frames"] += 1
                if snr > ping["peak"]:
                    ping["peak"], ping["doppler"] = snr, (k - search) * hz
            elif ping is not None and ft - ping["last"] > 0.1:
                dur = ping["last"] - ping["start"] + n / a.rate
                if dur >= a.min_duration and ping["frames"] >= 3:      # one noise spike isn't a meteor
                    emit({"event": "ping", "t": round(ping["start"], 3), "duration": round(dur, 3),
                          "snr": round(ping["peak"], 1), "doppler": round(ping["doppler"]), "wall": time.time()})
                ping = None
        t += len(x)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("hline", "meteors"))
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--input")
    ap.add_argument("--every", type=float, default=10.0)
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--search", type=float, default=1500.0, help="Hz either side of the carrier (Doppler)")
    ap.add_argument("--threshold", type=float, default=10.0)
    ap.add_argument("--min-duration", type=float, default=0.03)
    a = ap.parse_args(argv)
    run_hline(a) if a.what == "hline" else run_meteors(a)


if __name__ == "__main__":
    main()
