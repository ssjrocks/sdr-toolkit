"""Marine DSC (Digital Selective Calling) on VHF channel 70, as ITU-R M.493 specifies: distress alerts (with the
ship's position and what's wrong), urgency and safety calls, and the routine calls ships and coast stations
make to each other (which channel to talk on).

    python -m sdr_toolkit.engines.dsc [--rate 48000] < s16 FM audio

Reads the FM-demodulated channel (16-bit mono). The call is 1200 baud FSK on a 1700 Hz subcarrier: 2100 Hz is
the B state (0), 1300 Hz Y (1). A character is 10 bits: 7 information bits (least significant first) and the
number of B elements among them, 3 bits most significant first. After a dot pattern comes the phasing
sequence: symbol 125 in the DX positions, 111 down to 104 in the RX positions between them. Every character
of the call is then sent twice, its RX copy 4 characters after its DX one, and the call closes with an end of
sequence symbol (117, 122 or 127) and an error-check character (the even parity, bit by bit, of the format
specifier, the call's characters and the EOS).

Sync is found as M.493 § 3.3 asks, by recognising phasing characters (3 in their right positions) rather than
the dot pattern. Writes one JSON line per call to stdout: {"event": "call", ...see describe()}.
"""

import argparse
import json
import sys

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy.signal import firwin, lfilter

BAUD = 1200
CENTRE = 1700.0
PHASING_DX = 125
PHASING_RX = (111, 110, 109, 108, 107, 106, 105, 104)       # RX7 first
EOS = (117, 122, 127)
MAX_CHARS = 48

FORMATS = {102: "Geographic area", 112: "Distress", 114: "Group", 116: "All ships", 120: "Individual",
           123: "Individual (automatic)"}
CATEGORIES = {100: "Routine", 108: "Safety", 110: "Urgency", 112: "Distress"}
NATURE = {100: "Fire, explosion", 101: "Flooding", 102: "Collision", 103: "Grounding",
          104: "Listing, in danger of capsizing", 105: "Sinking", 106: "Disabled and adrift",
          107: "Undesignated distress", 108: "Abandoning ship", 109: "Piracy / armed robbery attack",
          110: "Man overboard", 112: "EPIRB emission"}
TC1 = {100: "F3E/G3E all modes telephony", 101: "F3E/G3E duplex telephony", 103: "Polling",
       104: "Unable to comply", 105: "End of call", 106: "Data", 109: "J3E telephony",
       110: "Distress acknowledgement", 112: "Distress relay", 113: "F1B/J2B TTY-FEC", 115: "F1B/J2B TTY-ARQ",
       118: "Test", 121: "Ship position or location registration updating", 126: "No information"}
TC2 = {100: "No reason given", 101: "Congestion at maritime switching centre", 102: "Busy",
       103: "Queue indication", 104: "Station barred", 105: "No operator available",
       106: "Operator temporarily unavailable", 107: "Equipment disabled", 108: "Unable to use proposed channel",
       109: "Unable to use proposed mode", 110: "Ships and aircraft (Resolution 18)", 111: "Medical transports",
       112: "Pay-phone / public call office", 113: "Facsimile / data (M.1081)", 126: "No information"}
EOS_NAMES = {117: "acknowledgement requested", 122: "acknowledgement", 127: "end of sequence"}


# ---- characters ------------------------------------------------------------------------------------


def encode(symbol: int) -> list[int]:
    """A symbol's 10 bits in the order sent."""
    info = [(symbol >> i) & 1 for i in range(7)]
    zeros = 7 - sum(info)
    return info + [(zeros >> 2) & 1, (zeros >> 1) & 1, zeros & 1]


def symbols_at(bits: np.ndarray) -> np.ndarray:
    """The symbol that starts at every bit offset (-1 where the 10 bits don't check)."""
    if len(bits) < 10:
        return np.zeros(0, int)
    w = sliding_window_view(bits.astype(np.int64), 10)
    info = w[:, :7] @ (1 << np.arange(7))
    check = w[:, 7] * 4 + w[:, 8] * 2 + w[:, 9]
    ok = check == 7 - w[:, :7].sum(axis=1)
    return np.where(ok, info, -1)


# ---- the call's content ----------------------------------------------------------------------------


def mmsi(chars) -> str:
    """Five characters (two digits each) -> the 9-digit identity (the tenth digit is 0)."""
    return "".join(f"{c:02d}" for c in chars)[:9]


def station_kind(m: str) -> str:
    if m.startswith("00"):
        return "coast station"
    if m.startswith("0"):
        return "group"
    if m.startswith("111"):
        return "SAR aircraft"
    if m.startswith(("970", "972", "974")):
        return {"970": "AIS-SART", "972": "man-overboard device", "974": "EPIRB-AIS"}[m[:3]]
    if m.startswith("8"):
        return "handheld"
    if m.startswith("98"):
        return "craft associated with a ship"
    return "ship"


def position(chars):
    """Ten digits: quadrant (0 NE, 1 NW, 2 SE, 3 SW), latitude ddmm, longitude dddmm -> (lat, lon) or None."""
    d = "".join(f"{c:02d}" for c in chars)
    if len(d) != 10 or d == "9" * 10 or d[0] not in "0123":
        return None
    lat = int(d[1:3]) + int(d[3:5]) / 60
    lon = int(d[5:8]) + int(d[8:10]) / 60
    if lat > 90 or lon > 180:
        return None
    q = int(d[0])
    return (-lat if q in (2, 3) else lat), (-lon if q in (1, 3) else lon)


def utc(chars):
    d = "".join(f"{c:02d}" for c in chars)
    return None if d == "8888" or len(d) != 4 else f"{d[:2]}:{d[2:]} UTC"


def frequency(chars) -> str:
    """A channel or frequency element (3 characters, units first)."""
    if all(c == 126 for c in chars):
        return ""
    d = "".join(f"{c:02d}" for c in reversed(chars))      # HM TM M H T U
    if d[0] == "9":
        ch = int(d[3:])
        mode = {"1": " (ship frequency simplex)", "2": " (coast frequency simplex)"}.get(d[2], "")
        return f"VHF channel {ch}{mode}"
    if d[0] == "3":
        return f"HF/MF channel {int(d[1:])}"
    return f"{int(d) / 10:.1f} kHz"


def describe(fmt: int, body: list[int], eos: int) -> dict:
    """The fields of a call: *body* is what came between the format specifier and the EOS."""
    out = {"format_code": fmt, "format": FORMATS.get(fmt, f"format {fmt}"), "eos": EOS_NAMES.get(eos, str(eos))}
    i = 0

    def take(n):
        nonlocal i
        part = body[i:i + n]
        i += n
        return part if len(part) == n else None

    if fmt in (120, 114, 123, 102):
        a = take(5)
        if a is None:
            return out
        if fmt == 102:
            out["area"] = "".join(f"{c:02d}" for c in a)
        else:
            out["to"] = mmsi(a)
            out["to_kind"] = station_kind(out["to"])
    if fmt != 112:
        c = take(1)
        if c is None:
            return out
        out["category"] = CATEGORIES.get(c[0], str(c[0]))
    s = take(5)
    if s is None:
        return out
    out["from"] = mmsi(s)
    out["from_kind"] = station_kind(out["from"])

    def distress_info(with_id):
        if with_id:
            d = take(5)
            if d is None:
                return
            out["distress_id"] = "" if all(x == 126 for x in d) else mmsi(d)
        n, p, t, sub = take(1), take(5), take(2), take(1)
        if n:
            out["nature"] = NATURE.get(n[0], str(n[0]))
        if p and position(p):
            out["lat"], out["lon"] = (round(v, 4) for v in position(p))
        if t and utc(t):
            out["time"] = utc(t)
        if sub:
            out["subsequent"] = TC1.get(sub[0], str(sub[0]))

    if fmt == 112:
        distress_info(False)
        return out
    t1 = take(1)
    if t1 is None:
        return out
    out["tc1"] = TC1.get(t1[0], str(t1[0]))
    if out.get("category") == "Distress" and t1[0] in (110, 112):
        distress_info(True)
        return out
    t2 = take(1)
    if t2 is None:
        return out
    out["tc2"] = TC2.get(t2[0], str(t2[0]))
    rest = body[i:]
    if len(rest) >= 6 and rest[0] == 55:                  # the ship's position (55, then 5 characters)
        p = position(rest[1:6])
        if p:
            out["lat"], out["lon"] = (round(v, 4) for v in p)
        rest = rest[6:]
    freqs = [frequency(rest[k:k + 3]) for k in range(0, len(rest) - 2, 3)]
    freqs = [f for f in freqs if f]
    if freqs:
        out["channel"] = freqs[0]
        if len(freqs) > 1:
            out["channel_tx"] = freqs[1]
    return out


def summary(c: dict) -> str:
    who = c.get("from", "?")
    parts = [c.get("category") or c["format"]]
    if c["format_code"] == 112:
        parts = ["DISTRESS"]
    parts.append(f"from {who}")
    if c.get("to"):
        parts.append(f"to {c['to']}")
    elif c["format_code"] == 116:
        parts.append("to all ships")
    for k in ("tc1", "nature", "channel"):
        if c.get(k) and c[k] != "No information":
            parts.append(c[k])
    if "lat" in c:
        parts.append(f"at {c['lat']:.4f}, {c['lon']:.4f}")
    if c.get("time"):
        parts.append(c["time"])
    return " · ".join(parts)


# ---- demodulation ----------------------------------------------------------------------------------


class Demodulator:
    """FM audio in, calls out. Keeps a few seconds of audio and looks for phasing in what's new."""

    def __init__(self, rate=48000):
        self.rate = int(rate)
        self.spb = self.rate / BAUD
        self.lp = firwin(int(self.rate / 600) | 1, 900, fs=self.rate)
        self.keep = int(4 * self.rate)
        self.audio = np.zeros(0, np.float64)
        self.t0 = 0                                       # sample number of self.audio[0]
        self.done_until = 0                               # samples up to here are decoded
        self.phase = 0.0

    def _soft(self, audio):
        """+ for Y (1300 Hz, a 1), - for B (2100 Hz, a 0)."""
        n = np.arange(len(audio))
        z = lfilter(self.lp, 1.0, audio * np.exp(-2j * np.pi * CENTRE * n / self.rate))
        f = np.angle(z[1:] * np.conj(z[:-1]))
        return -np.concatenate([[0.0], f])

    def feed(self, audio: np.ndarray) -> list[dict]:
        self.audio = np.concatenate([self.audio, np.asarray(audio, np.float64)])
        if len(self.audio) > self.keep:
            cut = len(self.audio) - self.keep
            self.audio = self.audio[cut:]
            self.t0 += cut
        if len(self.audio) < self.spb * 200:
            return []
        soft = self._soft(self.audio)
        c = np.concatenate([[0.0], np.cumsum(soft)])
        spb = self.spb
        found = []
        for p in range(0, int(spb), 2):
            starts = (p + np.arange(int((len(soft) - p) / spb)) * spb).astype(int)
            if len(starts) < 200:
                continue
            ends = np.minimum(starts + int(spb), len(soft))
            # Integrate the middle 80% of each bit (the edges are where the tones change).
            a, b = starts + int(spb * 0.1), ends - int(spb * 0.1)
            bits = (c[b] - c[a] > 0).astype(np.int8)
            found += list(self._calls(symbols_at(bits), starts, p))
        if not found:
            return []
        # The earliest call; of the bit phases that read it, one whose error check passed, with most phasing.
        first = min(f["_end"] for f in found)
        same = [f for f in found if f["_end"] <= first + spb * 100]
        best = max(same, key=lambda f: (f["ecc_ok"], f["_score"], -abs(f["_phase"] - spb / 2)))
        self.done_until = best["_end"]
        return [{k: v for k, v in best.items() if not k.startswith("_")}]

    def _calls(self, sym, starts, phase):
        """Calls in one bit phase's symbols that end after what's already been decoded."""
        n = len(sym)
        expected = []
        for k in range(16):
            if k % 2 == 0 and k < 12:
                expected.append((k, PHASING_DX))
            elif k % 2 == 1:
                expected.append((k, PHASING_RX[k // 2]))
        offsets = [k * 10 for k, _ in expected]
        last = n - 1 - max(offsets)
        if last <= 0:
            return
        idx = np.arange(last)
        score = np.zeros(last, int)
        for (k, want), off in zip(expected, offsets):
            score += sym[idx + off] == want
        for i in np.nonzero(score >= 3)[0]:
            if self.t0 + starts[i] < self.done_until:
                continue
            call = self._decode(sym, i + 120, starts)
            if call is None:
                continue
            call["_score"] = int(score[i])
            call["_phase"] = phase
            yield call
            return                                       # one call per look; the next look finds the next

    def _decode(self, sym, m, starts):
        """The call whose first format specifier (DX) starts at bit m, or None if it isn't all here yet."""
        def char(j):
            dx_i, rx_i = m + 20 * j, m + 50 + 20 * j
            dx = sym[dx_i] if dx_i < len(sym) else -2
            rx = sym[rx_i] if rx_i < len(sym) else -2
            if dx >= 0 and (rx < 0 or rx == dx):
                return int(dx), rx == dx
            if rx >= 0:
                return int(rx), dx == rx
            return (None, False) if dx == -1 or rx == -1 else ("more", False)

        chars, errors = [], 0
        for j in range(MAX_CHARS):
            c, both = char(j)
            if c == "more":
                return None                               # wait for more audio
            if c is None:
                errors += 1
                if errors > 3:
                    return None
            chars.append(c)
            if c in EOS and j >= 3:
                ecc, _ = char(j + 1)
                if ecc == "more":
                    return None
                break
        else:
            return None
        fmt = chars[0] if chars[0] is not None else chars[1]
        if fmt not in FORMATS:
            return None
        eos = chars[-1]
        body = chars[2:-1]
        info = [fmt] + body + [eos]
        ecc_ok = None not in info and ecc is not None and int(np.bitwise_xor.reduce(np.array(info))) == ecc
        out = describe(fmt, [c if c is not None else 126 for c in body], eos)
        out.update(event="call", ecc_ok=bool(ecc_ok), symbols=[c if c is not None else -1 for c in info + [ecc]])
        end_bit = min(m + 50 + 20 * (len(chars) + 1), len(starts) - 1)
        out["_end"] = self.t0 + int(starts[end_bit])
        out["summary"] = summary(out)
        return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=48000)
    a = ap.parse_args(argv)
    dem = Demodulator(a.rate)
    src = sys.stdin.buffer
    step = int(a.rate * 0.25) * 2
    while True:
        data = src.read(step)
        if not data:
            break
        x = np.frombuffer(data[:len(data) // 2 * 2], "<i2").astype(np.float64) / 32768.0
        for call in dem.feed(x):
            sys.stdout.write(json.dumps(call) + "\n")
            sys.stdout.flush()
    # The last call may need the samples still buffered: feed silence so it completes.
    for call in dem.feed(np.zeros(int(a.rate * 1.0))):
        sys.stdout.write(json.dumps(call) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
