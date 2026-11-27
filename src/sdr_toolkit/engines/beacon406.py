"""406 MHz distress beacons (COSPAS-SARSAT EPIRBs, PLBs and ELTs), first generation (C/S T.001): who the beacon
belongs to (its country, MMSI, aircraft address or serial), its 15-hex ID and, for location protocols, the GPS
position it sends.

    python -m sdr_toolkit.engines.beacon406 --input - | FILE --rate R --centre Hz

Reads cu8 IQ around --centre. The 406.0-406.1 MHz band is mixed down and watched for bursts: a beacon sends
every 50 s an unmodulated carrier for 160 ms, then 112 or 144 bits at 400 bit/s, biphase-L phase modulation of
+-1.1 rad. A burst's carrier is found, its phase locked from the 160 ms of carrier, and the bits read from the
phase of each half bit. The frame starts with 15 ones (bit sync) and a frame sync (000101111, or 011010000 for
a self-test); BCH-1 (bits 25-106) and, on long frames, BCH-2 (107-144) are checked before it counts.

The frame decoding follows dec406 (github.com/moricef/dec406, MIT licence, (c) 2026 Fabrice Morel, from F4EHY's
dec406_v7); second-generation (spread-spectrum) beacons are dec406's own decoder's job.

Writes JSON lines to stdout:
  {"event": "beacon", "freq": Hz, "hex_id": "...", "country": "...", "protocol": "...", "id": "...",
   "lat": .., "lon": .., "self_test": bool, "bch_ok": bool, "bits": "...", "level": dB}
"""

import argparse
import json
import sys

import numpy as np
from scipy.signal import firwin, lfilter

from ..receiver import dsp
from .beacon_countries import COUNTRIES

BAND_LO, BAND_HI = 406.0e6, 406.1e6
BAND_RATE = 120000                  # the band, mixed to 0 Hz
BIT_RATE = 400
DEMOD_RATE = 12000                  # a burst's channel: 30 samples a bit (BAND_RATE / 10)
SYNC_NORMAL, SYNC_TEST = "000101111", "011010000"
G1 = "1001101101100111100011"       # BCH-1 generator (21 parity bits)
G2 = "1010100111001"                # BCH-2 generator (12 parity bits)
MIN_SNR = 10.0                      # dB above the band's noise for a burst
BURST_MIN, BURST_MAX = 0.30, 0.90   # s

LOCATION = {2: "Standard location: EPIRB (MMSI)", 3: "Standard location: ELT (24-bit address)",
            4: "Standard location: ELT (serial)", 5: "Standard location: ELT (operator)",
            6: "Standard location: EPIRB (serial)", 7: "Standard location: PLB (serial)",
            8: "National location: ELT", 9: "ELT(DT) location", 10: "National location: EPIRB",
            11: "National location: PLB", 12: "Ship security", 13: "Return-link service (RLS) location",
            14: "Standard test", 15: "National test"}
USER = {0: "Orbitography", 1: "Aviation user", 2: "Maritime user", 3: "Serial user", 4: "National user",
        6: "Radio call sign user", 7: "Test user"}
SERIAL_TYPES = {0: "ELT", 1: "ELT (operator)", 2: "Float-free EPIRB", 3: "ELT (24-bit address)",
                4: "Non-float-free EPIRB", 6: "PLB"}
BAUDOT = {56: "A", 51: "B", 46: "C", 50: "D", 48: "E", 54: "F", 43: "G", 37: "H", 44: "I", 58: "J", 62: "K",
          41: "L", 39: "M", 38: "N", 35: "O", 45: "P", 61: "Q", 42: "R", 52: "S", 33: "T", 60: "U", 47: "V",
          57: "W", 55: "X", 53: "Y", 49: "Z", 36: " ", 24: "-", 23: "/", 13: "0", 29: "1", 25: "2", 16: "3",
          10: "4", 1: "5", 21: "6", 28: "7", 12: "8", 3: "9"}


# ---- the frame ---------------------------------------------------------------------------------------


def _rem(bits: str, g: str) -> str:
    """The remainder of bits (MSB first) divided by g, as len(g) - 1 bits."""
    r = [int(b) for b in bits]
    gi = [int(b) for b in g]
    for i in range(len(r) - len(g) + 1):
        if r[i]:
            for j, gb in enumerate(gi):
                r[i + j] ^= gb
    return "".join(str(b) for b in r[-(len(g) - 1):])


def bch1_ok(f: str) -> bool:
    return _rem(f[24:106], G1) == "0" * 21


def bch2_ok(f: str) -> bool:
    return len(f) < 144 or _rem(f[106:144], G2) == "0" * 12


def with_bch(f: str) -> str:
    """Bits 1-85 (and 107-132 for a long frame) -> the frame with its BCH parity filled in (for tests)."""
    pdf1 = f[24:85]
    out = f[:85] + _rem(pdf1 + "0" * 21, G1)
    if len(f) >= 132:
        pdf2 = f[106:132]
        out += pdf2 + _rem(pdf2 + "0" * 12, G2)
    else:
        out += f[106:112]
    return out


def bits_of(f: str, start: int, n: int) -> int:
    """Bits start..start+n-1 counting from 1, as T.001 numbers them."""
    return int(f[start - 1:start - 1 + n], 2)


def baudot(f: str, start: int, n: int) -> str:
    return "".join(BAUDOT.get(bits_of(f, start + 6 * j, 6), "_") for j in range(n)).strip()


def hex15(f: str, location_kind: str) -> str:
    """The 15-hex beacon ID: bits 26-85 with the position bits at their defaults."""
    v = int(f[25:85], 2)
    lo, hi = v & 0xFFFFFFFF, v >> 32
    if location_kind == "standard":
        lo = (lo & 0xFFE00000) | 0x000FFBFF
    elif location_kind == "national":
        lo = (lo & 0xF8000000) | 0x03F81FE0
    elif location_kind in ("rls", "eltdt"):
        lo = (lo & 0xFFF80000) | 0x0003FDFF
    return f"{hi:07X}{lo:08X}"


def _valid(lat, lon):
    return -90 <= lat <= 90 and -180 <= lon <= 180


def decode_frame(f: str) -> dict:
    """A frame (112 or 144 bits, from the bit sync) -> its fields."""
    long_ = len(f) >= 144
    out = {"self_test": f[15:24] == SYNC_TEST, "long": long_,
           "bch_ok": bch1_ok(f) and (not long_ or bits_of(f, 37, 3) == 0 and f[25] == "1" or bch2_ok(f))}
    cc = bits_of(f, 27, 10)
    out["country_code"] = cc
    out["country"] = COUNTRIES.get(cc, f"code {cc}")
    pos = None
    kind = ""
    if f[25] == "0" and long_:                           # location protocols
        pc = bits_of(f, 37, 4)
        out["protocol"] = LOCATION.get(pc, f"location protocol {pc}")
        ident = bits_of(f, 41, 24)
        if pc in (2, 12):
            out["id"] = f"MMSI ...{(ident >> 4) & 0xFFFFF:06d}" + (f", beacon {ident & 0xF}" if pc == 2 else "")
        elif pc == 3:
            out["id"] = f"Aircraft {ident:06X}"
            out["icao"] = f"{ident:06X}"
        elif pc in (4, 6, 7):
            out["id"] = f"Type approval {(ident >> 14) & 0x3FF}, serial {ident & 0x3FFF}"
        elif pc == 5:
            out["id"] = f"Operator {(ident >> 9) & 0x7FFF:05X}, serial {ident & 0x1FF}"
        if pc in (2, 3, 4, 5, 6, 7, 12, 14):
            kind = "standard"
            lat = bits_of(f, 66, 9) * 0.25 * (-1 if f[64] == "1" else 1)
            lon = bits_of(f, 76, 10) * 0.25 * (-1 if f[74] == "1" else 1)
            if bits_of(f, 107, 4) == 0b1101:
                out["gps"] = "internal" if f[110] == "1" else "external"
                out["homing_121"] = f[111] == "1"
                sl = 1 if f[112] == "1" else -1
                sg = 1 if f[122] == "1" else -1
                lat += sl * (bits_of(f, 114, 5) / 60 + bits_of(f, 119, 4) * 4 / 3600)
                lon += sg * (bits_of(f, 124, 5) / 60 + bits_of(f, 129, 4) * 4 / 3600)
            pos = (lat, lon) if pc != 14 else None
        elif pc in (8, 10, 11, 15):
            kind = "national"
            out["id"] = f"National ID {bits_of(f, 41, 18)}"
            lat = (bits_of(f, 60, 7) + bits_of(f, 67, 5) * 2 / 60) * (-1 if f[58] == "1" else 1)
            lon = (bits_of(f, 73, 8) + bits_of(f, 81, 5) * 2 / 60) * (-1 if f[71] == "1" else 1)
            if f[109] == "1":
                sl = 1 if f[112] == "1" else -1
                sg = 1 if f[119] == "1" else -1
                lat += sl * (bits_of(f, 114, 2) / 60 + bits_of(f, 116, 4) * 4 / 3600)
                lon += sg * (bits_of(f, 121, 2) / 60 + bits_of(f, 123, 4) * 4 / 3600)
            pos = (lat, lon) if pc != 15 else None
        elif pc == 9:
            kind = "eltdt"
            if bits_of(f, 41, 2) == 0:
                out["icao"] = f"{bits_of(f, 43, 24):06X}"
                out["id"] = f"Aircraft {out['icao']}"
            lat = bits_of(f, 68, 8) * 0.5 * (-1 if f[66] == "1" else 1)
            lon = bits_of(f, 77, 9) * 0.5 * (-1 if f[75] == "1" else 1)
            out["activation"] = ("manual", "automatic (G-switch)", "automatic (external)", "spare")[bits_of(f, 107, 2)]
            if bits_of(f, 113, 2):
                sl = 1 if f[114] == "1" else -1
                sg = 1 if f[123] == "1" else -1
                lat += sl * (bits_of(f, 116, 4) / 60 + bits_of(f, 120, 4) * 4 / 3600)
                lon += sg * (bits_of(f, 125, 4) / 60 + bits_of(f, 129, 4) * 4 / 3600)
            pos = (lat, lon)
        elif pc == 13:
            kind = "rls"
            lat_raw, lon_raw = bits_of(f, 67, 9), bits_of(f, 76, 10)
            if not (lat_raw == 0x0FF and lon_raw == 0x1FF):
                pos = (bits_of(f, 68, 8) * 0.5 * (-1 if f[66] == "1" else 1),
                       bits_of(f, 77, 9) * 0.5 * (-1 if f[75] == "1" else 1))
    else:                                                # user (and user-location) protocols
        pc = bits_of(f, 37, 3)
        out["protocol"] = USER.get(pc, f"user protocol {pc}") + (" (with location)" if long_ and pc else "")
        if pc == 2:
            out["id"] = f"Call sign / MMSI {baudot(f, 40, 6)}, beacon {baudot(f, 76, 1)}"
        elif pc in (1, 6):
            out["id"] = ("Aircraft registration " if pc == 1 else "Call sign ") + baudot(f, 40, 7)
        elif pc == 3:
            t = bits_of(f, 40, 3)
            out["id"] = f"{SERIAL_TYPES.get(t, 'beacon')} serial {bits_of(f, 44, 20)}"
            if f[42] == "1" and bits_of(f, 74, 10):
                out["id"] += f", C/S certificate {bits_of(f, 74, 10)}"
        elif pc == 0:
            out["id"] = "Orbitography (a COSPAS-SARSAT reference beacon)"
        if long_ and pc:
            out["gps"] = "internal" if f[106] == "1" else "external"
            lat = (bits_of(f, 109, 7) + bits_of(f, 116, 4) * 4 / 60) * (1 if f[107] == "0" else -1)
            lon = (bits_of(f, 121, 8) + bits_of(f, 129, 4) * 4 / 60) * (1 if f[119] == "0" else -1)
            pos = (lat, lon)
    if pos and _valid(*pos) and not (abs(pos[0]) > 90 - 1e-9 and kind == "standard"):
        out["lat"], out["lon"] = round(pos[0], 5), round(pos[1], 5)
    out["hex_id"] = hex15(f, kind)
    return out


# ---- the signal ------------------------------------------------------------------------------------


def read_burst(x: np.ndarray, rate: float) -> str | None:
    """A burst's complex baseband (carrier near 0 Hz, starting a little before the carrier) -> its frame bits
    from the bit sync, or None."""
    spb = rate / BIT_RATE
    half = int(round(spb / 2))
    mag = np.abs(x)
    on = np.nonzero(mag > 0.5 * np.percentile(mag, 90))[0]
    if not len(on):
        return None
    s0 = on[0]
    cw = x[s0 + int(0.02 * rate):s0 + int(0.14 * rate)]
    if len(cw) < 100:
        return None
    # Residual frequency from the carrier's phase slope, then its phase as the reference.
    f_res = np.angle(np.sum(cw[1:] * np.conj(cw[:-1]))) * rate / (2 * np.pi)
    n = np.arange(len(x))
    y = x * np.exp(-2j * np.pi * f_res * n / rate)
    ref = np.angle(np.sum(y[s0 + int(0.02 * rate):s0 + int(0.14 * rate)]))
    y = y * np.exp(-1j * ref)
    q = np.imag(y) / (np.abs(y) + 1e-12)               # sin(phase): +-0.89 during data, 0 on the carrier
    c = np.concatenate([[0.0], np.cumsum(q)])
    best = None
    lo = s0 + int(0.12 * rate)
    hi = s0 + int(0.22 * rate)
    for start in range(lo, hi):
        need = start + 144 * int(spb) + 2
        if need >= len(c) and start + 112 * int(spb) + 2 >= len(c):
            break
        k = start + np.arange(min(144, int((len(c) - 1 - start) / spb))) * spb
        k = k.astype(int)
        s1 = c[k + half] - c[k]
        s2 = c[np.minimum(k + 2 * half, len(c) - 1)] - c[k + half]
        soft = s1 - s2
        if len(soft) < 112:
            break
        for sign in (1, -1):
            bits = "".join("1" if v * sign > 0 else "0" for v in soft)
            if bits[:15] == "1" * 15 and bits[15:24] in (SYNC_NORMAL, SYNC_TEST):
                strength = float(np.mean(np.abs(soft[:24])))
                if best is None or strength > best[0]:
                    best = (strength, bits)
    if best is None:
        return None
    bits = best[1]
    return bits[:144] if bits[24] == "1" and len(bits) >= 144 else bits[:112]


class Watcher:
    """Finds bursts in the band and reads them. Times are in samples of the band."""

    FRAME = 0.02                                       # s per power frame

    def __init__(self, rate=BAND_RATE):
        self.rate = rate
        self.nf = int(self.FRAME * rate)
        self.buf = np.zeros(0, np.complex64)
        self.t0 = 0
        self.nfft = 256
        self.bin_hz = rate / self.nfft
        self.active: dict[int, int] = {}                # bin -> frame it started
        self.frame_no = 0
        self.floor = None
        self.power_hist: list[np.ndarray] = []

    def feed(self, x):
        out = []
        self.buf = np.concatenate([self.buf, x])
        while len(self.buf) - (self.frame_no * self.nf - self.t0) >= self.nf:
            i = self.frame_no * self.nf - self.t0
            frame = self.buf[i:i + self.nf]
            spec = np.abs(np.fft.fftshift(np.fft.fft(frame[:self.nfft] * np.hanning(self.nfft)))) ** 2
            self.power_hist = (self.power_hist + [spec])[-250:]
            floor = np.median(np.array(self.power_hist), axis=0) + 1e-20
            lit = spec > floor * 10 ** (MIN_SNR / 10)
            lit_bins = set(np.nonzero(lit)[0])
            for b in list(self.active):
                if not ({b - 1, b, b + 1} & lit_bins):
                    start = self.active.pop(b)
                    dur = (self.frame_no - start) * self.FRAME
                    if BURST_MIN <= dur <= BURST_MAX:
                        out.append((start, self.frame_no, b))
            for b in lit_bins:
                if not any(abs(b - a) <= 1 for a in self.active):
                    self.active[b] = self.frame_no
            self.frame_no += 1
        # One burst lights several neighbouring bins (its modulation, the window's sidelobes): the longest
        # of those that overlap in time and lie within 4 kHz stands for them.
        out.sort(key=lambda o: o[0] - o[1])
        chosen = []
        for o in out:
            if not any(o[0] < c[1] and c[0] < o[1] and abs(o[2] - c[2]) * self.bin_hz < 4000 for c in chosen):
                chosen.append(o)
        results = []
        for start, end, b in chosen:
            r = self._read(start, end, b)
            if r:
                results.append(r)
        # Keep 2 s for bursts still going on.
        keep = int(2.0 * self.rate)
        if len(self.buf) > keep:
            cut = len(self.buf) - keep
            self.buf = self.buf[cut:]
            self.t0 += cut
        return results

    def _read(self, start, end, b):
        a = max(0, start * self.nf - self.t0 - int(0.05 * self.rate))
        z = min(len(self.buf), end * self.nf - self.t0 + int(0.1 * self.rate))
        seg = self.buf[a:z]
        if len(seg) < int(0.3 * self.rate):
            return None
        offset = (b - self.nfft // 2) * self.bin_hz
        # The carrier exactly: the strongest bin of a long FFT of the first 140 ms around this one.
        head = seg[int(0.05 * self.rate):int(0.19 * self.rate)]
        n = 1 << 15
        spec = np.abs(np.fft.fftshift(np.fft.fft(head, n)))
        freqs = (np.arange(n) - n // 2) * self.rate / n
        near = np.abs(freqs - offset) < 2 * self.bin_hz
        f = freqs[near][np.argmax(spec[near])]
        t = np.arange(len(seg))
        y = seg * np.exp(-2j * np.pi * f * t / self.rate)
        d = self.rate // DEMOD_RATE
        y = lfilter(firwin(129, 1500, fs=self.rate), 1.0, y)[::d]
        y = y[64 // d:]
        bits = read_burst(y, DEMOD_RATE)
        if bits is None:
            return None
        level = 10 * np.log10(np.mean(np.abs(seg) ** 2) + 1e-20)
        return {"offset": float(f), "bits": bits, "level": round(float(level), 1)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="-")
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--centre", type=float, required=True)
    a = ap.parse_args(argv)
    mid = (BAND_LO + BAND_HI) / 2
    if abs(mid - a.centre) + 60000 > a.rate / 2:
        sys.exit("beacon406: 406.0-406.1 MHz isn't inside what the SDR sees")
    if a.rate % BAND_RATE:
        sys.exit(f"beacon406: the sample rate must be a multiple of {BAND_RATE}")
    chz = dsp.Channelizer(a.rate, m=512, out_rate=BAND_RATE)
    chz.set("band", mid - a.centre, 110000)
    w = Watcher()
    src = sys.stdin.buffer if a.input == "-" else open(a.input, "rb")
    sys.stdout.write(json.dumps({"event": "start", "band": [BAND_LO, BAND_HI]}) + "\n")
    sys.stdout.flush()
    while True:
        data = src.read(1 << 19)
        if not data or len(data) < 2:
            break
        y = chz.process(dsp.cu8(data[:len(data) // 2 * 2])).get("band")
        if y is None or not len(y):
            continue
        for r in w.feed(y):
            fields = decode_frame(r["bits"])
            fields.update(event="beacon", freq=round(mid + r["offset"]), bits=r["bits"], level=r["level"])
            sys.stdout.write(json.dumps(fields) + "\n")
            sys.stdout.flush()
    for r in w.feed(np.zeros(int(1.0 * BAND_RATE), np.complex64)):
        fields = decode_frame(r["bits"])
        fields.update(event="beacon", freq=round(mid + r["offset"]), bits=r["bits"], level=r["level"])
        sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
