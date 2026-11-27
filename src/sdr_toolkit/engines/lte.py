"""LTE cells around: which towers' cells are on air, on which channel (EARFCN) and band, their physical cell ID
(PCI), FDD or TDD, and how strong (3GPP TS 36.211 sec. 6.11: the synchronisation signals).

    python -m sdr_toolkit.engines.lte --rate R --centre HZ [--input FILE] [--ppm N]

Every 5 ms a cell sends its primary synchronisation signal (PSS: one of three Zadoff-Chu sequences, N_ID2) and
just before it the secondary one (SSS: two interleaved m-sequences, N_ID1 of 168 and which half of the frame);
PCI = 3 N_ID1 + N_ID2. Both sit on the middle 62 subcarriers (15 kHz apart), so 1.92 MS/s around the carrier's
centre holds them. For every 100 kHz raster point in view, the IQ is moved to that point (and a few kHz either
side, for the SDR's crystal), cut to 1.92 MS/s, and correlated with the three PSS; the correlation is folded over
5 ms so a cell's eight PSS in 40 ms add up. Where one stands out, the SSS one symbol before it (FDD), or three
(TDD), is matched against the 336 SSS of that N_ID2.

JSON lines out: {"event": "cell", "freq", "earfcn", "band", "pci", "n_id1", "n_id2", "duplex", "power", "pss_snr",
                 "sss_score", "freq_offset", "t"}
"""

import argparse
import json
import sys
import time

import numpy as np
from scipy.signal import fftconvolve

from ..receiver import dsp

FS = 1_920_000
NFFT = 128
HALF_FRAME = FS // 200                # 5 ms: 9600 samples
SSS_SURE = 3.5                        # an SSS match this many times the median: the PCI is trusted
SYM = NFFT + 9                        # an OFDM symbol and its normal cyclic prefix, at 1.92 MS/s
# Downlink bands: (F_DL_low MHz, F_DL_high MHz, N_offs-DL), 36.101 table 5.7.3-1.
BANDS = {1: (2110, 2170, 0), 2: (1930, 1990, 600), 3: (1805, 1880, 1200), 4: (2110, 2155, 1950), 5: (869, 894, 2400),
         7: (2620, 2690, 2750), 8: (925, 960, 3450), 12: (729, 746, 5010), 13: (746, 756, 5180), 20: (791, 821, 6150),
         28: (758, 803, 9210), 40: (2300, 2400, 38650), 41: (2496, 2690, 39650), 42: (3400, 3600, 41590),
         66: (2110, 2200, 66436), 71: (617, 652, 68586)}


def band_of(f_hz):
    mhz = f_hz / 1e6
    for b, (lo, hi, off) in BANDS.items():
        if lo <= mhz <= hi:
            return b, int(round(off + (mhz - lo) * 10))
    return None, None


def _carriers(seq):
    """62 values onto the 128 subcarriers around DC (DC itself unused): -31..-1, +1..+31."""
    X = np.zeros(NFFT, complex)
    for n, v in enumerate(seq):
        k = n - 31
        if k >= 0:
            k += 1
        X[k % NFFT] = v
    return X


def pss_seq(nid2):
    u = (25, 29, 34)[nid2]
    n = np.arange(62)
    m = np.where(n < 31, n, n + 1)
    return np.exp(-1j * np.pi * u * m * (m + 1) / 63)


def _mseq(taps):
    x = [0, 0, 0, 0, 1]
    for i in range(26):
        x.append(sum(x[i + t] for t in taps) % 2)
    return 1 - 2 * np.array(x[:31])


S_T, C_T, Z_T = _mseq((2, 0)), _mseq((3, 0)), _mseq((4, 2, 1, 0))


def sss_seq(nid1, nid2, sf5=False):
    qp = nid1 // 30
    q = (nid1 + qp * (qp + 1) // 2) // 30
    mp = nid1 + q * (q + 1) // 2
    m0 = mp % 31
    m1 = (m0 + mp // 31 + 1) % 31
    n = np.arange(31)
    s0, s1 = S_T[(n + m0) % 31], S_T[(n + m1) % 31]
    c0, c1 = C_T[(n + nid2) % 31], C_T[(n + nid2 + 3) % 31]
    z0, z1 = Z_T[(n + m0 % 8) % 31], Z_T[(n + m1 % 8) % 31]
    d = np.empty(62)
    if not sf5:
        d[0::2], d[1::2] = s0 * c0, s1 * c1 * z0
    else:
        d[0::2], d[1::2] = s1 * c0, s0 * c1 * z1
    return d


PSS_TIME = [np.fft.ifft(_carriers(pss_seq(k))) for k in range(3)]
_SSS = {}


def sss_table(nid2):
    if nid2 not in _SSS:
        _SSS[nid2] = np.array([[sss_seq(i, nid2, sf5) for sf5 in (False, True)] for i in range(168)])   # 168x2x62
    return _SSS[nid2]


def _sub(X):
    """The 62 subcarriers -31..-1, +1..+31 of a 128-point spectrum, in sequence order."""
    k = np.concatenate([np.arange(-31, 0), np.arange(1, 32)]) % NFFT
    return X[k]


def find_cells(x, rate, centre, offsets=(-10e3, -5e3, 0.0, 5e3, 10e3), min_snr=9.0):
    """Cells in a block of IQ (at least 20 ms): [dict]."""
    out = []
    span = rate * 0.42
    lo, hi = centre - span, centre + span
    raster = [f for f in np.arange(np.ceil(lo / 1e5) * 1e5, hi, 1e5) if band_of(f)[0] is not None]
    if not raster:
        return out
    # Every raster point cut to 1.92 MS/s at once (one FFT channelizer); the crystal-error offsets are then
    # tried on the cut channel, where they're cheap.
    if rate % FS:
        raise ValueError(f"the sample rate must be a multiple of {FS}")
    chz = dsp.Channelizer(rate, m=256, out_rate=FS)
    for f in raster:
        chz.set(float(f), float(f - centre), 1.2e6)
    chans = chz.process(np.asarray(x, np.complex64))
    for f in raster:
        band, earfcn = band_of(f)
        y0 = chans.get(float(f))
        if y0 is None or len(y0) < 4 * HALF_FRAME:
            continue
        t = np.arange(len(y0)) / FS
        best = None
        for off in offsets:
            y = y0 * np.exp(-2j * np.pi * off * t)
            n = len(y) // HALF_FRAME * HALF_FRAME
            for k in range(3):
                c = np.abs(fftconvolve(y[:n], np.conj(PSS_TIME[k][::-1]), mode="same")) ** 2
                folded = c.reshape(-1, HALF_FRAME).sum(axis=0)
                p = int(np.argmax(folded))
                snr = 10 * np.log10(folded[p] / (np.median(folded) + 1e-30))
                if best is None or snr > best[0]:
                    best = (snr, k, p, off, y)
        if best is None or best[0] < min_snr:
            continue
        snr, nid2, p, off, y = best
        # The rest of the crystal error (up to half a 5 kHz step) turns the SSS against the PSS channel estimate:
        # the cyclic prefix (each symbol's last 9 samples repeated before it) measures it to a few Hz.
        r = np.sum(y[:-NFFT] * np.conj(y[NFFT:]))
        resid = -float(np.angle(r)) * FS / (2 * np.pi * NFFT)          # y[n] y*[n+128] turns by -2 pi f 128 / FS
        if abs(resid) < 4000:
            y = y * np.exp(-2j * np.pi * resid * np.arange(len(y)) / FS)
            off += resid
        # p is where the PSS correlation peaks ("same": the centre of the template): its symbol starts NFFT/2
        # earlier. The SSS is one symbol before it (FDD) or three (TDD).
        start = p - NFFT // 2
        tbl = sss_table(nid2)
        best_s = None
        for duplex, back in (("FDD", SYM), ("TDD", 3 * SYM + 1)):
            acc = np.zeros((168, 2))
            for h in range(start, len(y) - NFFT, HALF_FRAME):
                a = h - back
                if a < 0:
                    continue
                P = _sub(np.fft.fft(y[h:h + NFFT]))
                S = _sub(np.fft.fft(y[a:a + NFFT]))
                ch = P / pss_seq(nid2)                                   # the channel, from the PSS
                eq = S * np.conj(ch) / (np.abs(ch) ** 2 + 1e-12)
                v = np.real(tbl @ eq) / 62                               # 168x2
                acc += np.abs(v)
            i, sf = np.unravel_index(int(np.argmax(acc)), acc.shape)
            score = float(acc[i, sf] / (np.median(acc) + 1e-12))
            if best_s is None or score > best_s[0]:
                best_s = (score, int(i), duplex)
        score, nid1, duplex = best_s
        sure = score >= SSS_SURE                                    # else only N_ID2 is known
        power = 10 * np.log10(np.mean(np.abs(y[start:start + NFFT]) ** 2) + 1e-20)
        out.append({"event": "cell", "freq": float(f), "earfcn": earfcn, "band": band,
                    "pci": 3 * nid1 + nid2 if sure else None, "n_id1": nid1 if sure else None, "n_id2": nid2,
                    "duplex": duplex if sure else "", "power": round(float(power), 1),
                    "pss_snr": round(float(snr), 1), "sss_score": round(score, 1), "freq_offset": round(off)})
    # One cell is found at its raster point and, weaker, at the ones next to it: keep the best per PCI nearby.
    # A detection whose SSS didn't stand out is kept only when its PSS is strong and no cell that did is within
    # 1.5 MHz (there it's that cell's shadow through the filters).
    sure = sorted((c for c in out if c["pci"] is not None), key=lambda c: -c["pss_snr"])
    keep = []
    for c in sure:
        if not any(abs(k["freq"] - c["freq"]) < 3e5 and k["pci"] == c["pci"] for k in keep):
            keep.append(c)
    for c in sorted((c for c in out if c["pci"] is None), key=lambda c: -c["pss_snr"]):
        # (A Zadoff-Chu PSS also correlates, shifted in time, a few raster points away under another root: one
        # real cell shows as a cluster of unsure detections, of which the strongest is kept.)
        if c["pss_snr"] >= 15 and not any(abs(k["freq"] - c["freq"]) < 1.5e6 for k in keep):
            keep.append(c)
    return keep


def windows(bands, rate):
    """Window centres covering the bands' downlinks with the middle 84% of each window."""
    out = []
    use = rate * 0.84
    for b in bands:
        lo, hi, _ = BANDS[b]
        n = max(1, int(np.ceil((hi - lo) * 1e6 / use)))
        step = (hi - lo) * 1e6 / n
        out += [lo * 1e6 + (i + 0.5) * step for i in range(n)]
    return out


def run_live(a):
    import socket
    import struct

    def cmd(c, v):
        return struct.pack(">BI", c, int(v) & 0xFFFFFFFF)
    try:
        sock = socket.create_connection(("127.0.0.1", a.port), timeout=10)
    except OSError as e:
        sys.exit(f"lte: couldn't reach the SDR's rtl_tcp: {e}")
    head = b""
    while len(head) < 12:
        d = sock.recv(12 - len(head))
        if not d:
            sys.exit("lte: the SDR's rtl_tcp closed the connection")
        head += d
    if a.gain:
        sock.sendall(cmd(3, 1) + cmd(4, int(float(a.gain) * 10)))
    sock.settimeout(10)
    bands = [int(b) for b in a.bands.split(",") if b.strip()]
    centres = windows(bands, a.rate)
    while True:
        for c in centres:
            f = int(c)
            sock.sendall(cmd(2, a.rate) + (cmd(0x40, f >> 32) if f >> 32 else b"") + cmd(1, f & 0xFFFFFFFF))
            emit({"event": "window", "centre": c, "lo": c - a.rate * 0.42, "hi": c + a.rate * 0.42})
            need = int(a.rate * (0.08 + a.ms / 1000)) * 2
            buf = bytearray()
            try:
                while len(buf) < need:
                    d = sock.recv(1 << 20)
                    if not d:
                        sys.exit("lte: the SDR's rtl_tcp closed the connection")
                    buf += d
            except TimeoutError:
                sys.exit("lte: no samples for 10 s: the SDR isn't streaming")
            x = dsp.cu8(bytes(buf[int(a.rate * 0.08) * 2:need]))           # 80 ms to settle after the retune
            for cell in find_cells(x, a.rate, c):
                cell["t"] = time.time()
                emit(cell)


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--centre", type=float)
    ap.add_argument("--input")
    ap.add_argument("--port", type=int)
    ap.add_argument("--bands", default="28,5,8,3,1,7,40")
    ap.add_argument("--gain", default="")
    ap.add_argument("--ms", type=float, default=40.0)
    a = ap.parse_args(argv)
    if a.rate % FS:
        sys.exit(f"lte: the sample rate must be a multiple of {FS}")
    if a.port:
        run_live(a)
        return
    src = sys.stdin.buffer if not a.input else open(a.input, "rb")
    need = int(a.rate * a.ms / 1000) * 2
    data = src.read(need)
    x = dsp.cu8(data[:len(data) // 2 * 2])
    for c in find_cells(x, a.rate, a.centre):
        c["t"] = time.time()
        emit(c)


if __name__ == "__main__":
    main()
