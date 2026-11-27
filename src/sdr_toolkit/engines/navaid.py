"""VOR and ILS beacons: a VOR's radial (the bearing from the station you're on), an ILS localizer's or glide
slope's course deviation, and each beacon's Morse ident.

    python -m sdr_toolkit.engines.navaid --input - | FILE --rate R --centre Hz [--freqs Hz,Hz,...]

Reads cu8 IQ around --centre. Every navaid channel in it (50 kHz apart: 108-117.95 MHz VORs and localizers,
328.6-335.4 MHz glide slopes; or the --freqs given) is cut out at 48 kHz by one FFT channelizer. A channel
whose carrier stands MIN_SNR above the band's noise is measured on its AM envelope every ANALYSE_EVERY s of
samples, over the last WINDOW s:

  VOR: a 30 Hz AM tone (the variable signal, from the rotating pattern) and a 9960 Hz subcarrier frequency-
       modulated +-480 Hz by a 30 Hz reference. At the station's magnetic north they're in phase; on radial R
       the variable lags the reference by R degrees (ICAO Annex 10). A Doppler VOR swaps which is AM and which
       FM, and its rotation, so a receiver reads it the same way.
  ILS: 90 Hz and 150 Hz AM tones (20% each on a localizer's course, 40% on a glide slope's path). The
       difference in depth of modulation (DDM, 90 minus 150) says which side you're on: 90 Hz dominates left
       of a localizer's course (seen from an aircraft approaching) and above a glide slope's path.
  Ident: a 1020 Hz tone keyed in Morse every 10-30 s, decoded by the toolkit's Morse decoder.

Writes JSON lines to stdout:
  {"event": "navaid", "freq": Hz, "kind": "VOR" | "LOC" | "GS" | "carrier", "snr": dB, "level": dBFS, ...}
     VOR: "radial" (degrees from the station), "var_depth", "ref_dev" (Hz). LOC / GS: "ddm", "sdm".
  {"event": "ident", "freq": Hz, "text": "SY"}
Times are counted in samples, so a recording read faster than real time behaves as it would live.
"""

import argparse
import json
import sys

import numpy as np
from scipy.signal import firwin, lfilter, resample_poly

from ..receiver import dsp

CH_RATE = 48000
SPACING = 50000
ANALYSE_EVERY = 2.0      # s of samples between measurements of a channel
WINDOW = 4.0             # s measured each time: whole cycles of 30, 90 and 150 Hz
MIN_SNR = 10.0           # dB above the band's noise floor
LEAK_DB = 25.0           # a plain carrier this far below a neighbour 50 kHz away is that neighbour's edge
SUBCARRIER = 9960.0
IDENT_TONE = 1020.0
# ILS: a localizer's frequency -> its glide slope's (ICAO Annex 10 pairing), MHz.
GS_PAIRS = {
    108.10: 334.70, 108.15: 334.55, 108.30: 334.10, 108.35: 333.95, 108.50: 329.90, 108.55: 329.75,
    108.70: 330.50, 108.75: 330.35, 108.90: 329.30, 108.95: 329.15, 109.10: 331.40, 109.15: 331.25,
    109.30: 332.00, 109.35: 331.85, 109.50: 332.60, 109.55: 332.45, 109.70: 333.20, 109.75: 333.05,
    109.90: 333.80, 109.95: 333.65, 110.10: 334.40, 110.15: 334.25, 110.30: 335.00, 110.35: 334.85,
    110.50: 329.60, 110.55: 329.45, 110.70: 330.20, 110.75: 330.05, 110.90: 330.80, 110.95: 330.65,
    111.10: 331.70, 111.15: 331.55, 111.30: 332.30, 111.35: 332.15, 111.50: 332.90, 111.55: 332.75,
    111.70: 333.50, 111.75: 333.35, 111.90: 331.10, 111.95: 330.95,
}


def is_localizer(mhz: float) -> bool:
    """108.10-111.95 MHz with an odd tenth: localizers (the even tenths are VORs)."""
    return 108.05 < mhz < 112.0 and int(round(mhz * 10)) % 2 == 1


def channels_in(centre: float, rate: float) -> list[float]:
    """The navaid channels (Hz) inside the middle 85% of what the SDR sees."""
    lo, hi = centre - rate * 0.425, centre + rate * 0.425
    out = []
    for band_lo, band_hi in ((108.0e6, 117.95e6), (328.6e6, 335.4e6)):
        f = max(band_lo, np.ceil(lo / SPACING) * SPACING)
        while f <= min(hi, band_hi) + 1:
            out.append(float(f))
            f += SPACING
    return out


def _tone(a, rate, f):
    """The complex amplitude of a tone at f in a (depth for an AM envelope a = env / mean - 1)."""
    t = np.arange(len(a)) / rate
    return 2 * np.mean(a * np.exp(-2j * np.pi * f * t))


_LP = firwin(161, 1100, fs=CH_RATE)          # the subcarrier's FM (+-480 Hz around 0 after mixing) and 30 Hz


def analyse(y: np.ndarray, mhz: float) -> dict:
    """Measure one channel's complex baseband y (48 kHz): its kind and reading."""
    env = np.abs(y).astype(np.float64)
    c = env.mean()
    if c <= 0:
        return {"kind": "carrier"}
    a = env / c - 1.0
    # Whole cycles only: trim to a multiple of 1/30 s (which holds 90 and 150 Hz too).
    n = len(a) // (CH_RATE // 30) * (CH_RATE // 30)
    a = a[-n:]
    m30, m90, m150 = (abs(_tone(a, CH_RATE, f)) for f in (30.0, 90.0, 150.0))
    t = np.arange(n) / CH_RATE
    sub = lfilter(_LP, 1.0, a * np.exp(-2j * np.pi * SUBCARRIER * t))[::10]
    lowa = lfilter(_LP, 1.0, a)[::10]             # the same filter and delay as the subcarrier's path
    sub, lowa = sub[20:], lowa[20:]               # the filter settling
    r2 = CH_RATE / 10
    sub_depth = 2 * float(np.mean(np.abs(sub)))
    out = {"m30": round(m30, 4), "m90": round(m90, 4), "m150": round(m150, 4), "sub": round(sub_depth, 4)}
    if sub_depth > 0.08 and m30 > 0.05:
        dev = np.angle(sub[1:] * np.conj(sub[:-1])) * r2 / (2 * np.pi)
        k = (len(dev) // int(r2 / 30)) * int(r2 / 30)
        dev, va = dev[-k:], lowa[1:][-k:]
        tt = np.arange(k) / r2
        w = np.exp(-2j * np.pi * 30.0 * tt)
        ref = 2 * np.mean(dev * w)
        var = 2 * np.mean(va * w)
        # The discriminator's output sits half a sample later than the envelope's: undo it.
        ref *= np.exp(2j * np.pi * 30.0 * 0.5 / r2)
        if 150 < abs(ref) < 900:
            radial = float(np.degrees(np.angle(ref) - np.angle(var)) % 360)
            out.update(kind="VOR", radial=round(radial, 1), var_depth=round(m30, 3), ref_dev=round(abs(ref), 0))
            return out
    sdm = m90 + m150
    if m90 > 0.03 and m150 > 0.03 and 0.15 < sdm < 1.1:
        kind = "GS" if mhz > 300 else "LOC"
        out.update(kind=kind, ddm=round(m90 - m150, 4), sdm=round(sdm, 3))
        return out
    out["kind"] = "carrier"
    return out


class Channel:
    def __init__(self, freq):
        from .morse.engine import Engine
        self.freq = freq
        self.buf = []
        self.n = 0
        self.since = 0
        self.morse = Engine(freq=IDENT_TONE, bandwidth=80.0, rate=8000)
        self.morse.decoder.mode = "any"           # idents are slow (about 7 WPM), at no fixed speed
        self.morse.decoder.last_unit = 1200 / 7
        self.ident_t = 0.0

    def add(self, y):
        self.buf.append(y)
        self.n += len(y)
        self.since += len(y)
        keep = int(WINDOW * CH_RATE)
        while self.n - len(self.buf[0]) >= keep:
            self.n -= len(self.buf.pop(0))

    def window(self):
        return np.concatenate(self.buf)[-int(WINDOW * CH_RATE):]

    def ident(self, y, now):
        """Feed the AM audio to the Morse decoder; the idents it finished."""
        env = np.abs(y).astype(np.float64)
        c = env.mean()
        if c <= 0:
            return []
        audio = resample_poly(env / c - 1.0, 1, 6).astype(np.float32)     # 8 kHz
        return [m.text.strip() for m in self.morse.feed(audio, now=now) if m.text.strip()]


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="-")
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--centre", type=float, required=True)
    ap.add_argument("--freqs", default="")
    a = ap.parse_args(argv)
    freqs = [float(f) for f in a.freqs.split(",") if f.strip()] or channels_in(a.centre, a.rate)
    if not freqs:
        sys.exit("navaid: no VOR / ILS channels in what the SDR sees: centre it in 108-118 or 328.6-335.4 MHz")
    # Long blocks: each costs one small inverse FFT per channel, and a HackRF's band holds 160 channels.
    chz = dsp.Channelizer(a.rate, m=1024, out_rate=CH_RATE)
    for f in freqs:
        chz.set(f, f - a.centre, 25000)
    # Noise probes: halfway between channels, where no navaid is (16 across the band are plenty).
    probes = [f + SPACING / 2 for f in freqs if abs(f + SPACING / 2 - a.centre) < a.rate * 0.43]
    probes = probes[::max(1, len(probes) // 16)]
    for p in probes:
        chz.set(("noise", p), p - a.centre, 6000)
    chans = {f: Channel(f) for f in freqs}
    src = sys.stdin.buffer if a.input == "-" else open(a.input, "rb")
    emit({"event": "start", "channels": [round(f / 1e6, 3) for f in freqs]})
    samples = 0
    block = 1 << 18
    while True:
        data = src.read(block * 2)
        if not data or len(data) < 2:
            break
        x = dsp.cu8(data[:len(data) // 2 * 2])
        samples += len(x)
        now = samples / a.rate
        out = chz.process(x)
        noise = [float(np.mean(np.abs(v) ** 2)) for k, v in out.items() if isinstance(k, tuple) and len(v)]
        floor = np.median(noise) * 25000 / 6000 if noise else None
        power = {f: float(np.mean(np.abs(out[f]) ** 2)) for f in chans if f in out and len(out[f])}
        for f, ch in chans.items():
            if f not in power:
                continue
            y = out[f]
            p = power[f]
            snr = 10 * np.log10(p / floor) if floor else 99.0
            if snr < MIN_SNR:
                ch.since = 0
                ch.buf, ch.n = [], 0
                continue
            ch.add(y)
            for text in ch.ident(y, now):
                emit({"event": "ident", "freq": f, "text": text})
            if ch.since >= ANALYSE_EVERY * CH_RATE and ch.n >= WINDOW * CH_RATE * 0.99:
                ch.since = 0
                r = analyse(ch.window(), f / 1e6)
                loudest = max(power.get(f - SPACING, 0.0), power.get(f + SPACING, 0.0))
                if r["kind"] == "carrier" and loudest > p * 10 ** (LEAK_DB / 10):
                    continue                      # a strong neighbour's edge, not a beacon of its own
                emit(dict(r, event="navaid", freq=f, snr=round(snr, 1), level=round(10 * np.log10(p + 1e-20), 1)))
    for f, ch in chans.items():
        msg = ch.morse.decoder.finish()
        if msg and msg.text.strip():
            emit({"event": "ident", "freq": f, "text": msg.text.strip()})


if __name__ == "__main__":
    main()
