"""IEEE 802.15.4 at 2.4 GHz (Zigbee, Thread, Matter over Thread): the frames smart-home devices send, read from
an SDR's IQ.

    python -m sdr_toolkit.engines.zigbee --rate 4000000 [--input FILE] [--channel N]

The PHY (802.15.4-2006 sec. 6.5): 4-bit symbols spread to 32 chips (2 Mchip/s), O-QPSK with half-sine pulses:
even chips on I, odd chips on Q half a symbol... one chip later. That is MSK, so an FM discriminator gives each
chip interval's frequency sign; the 16 symbols' discriminator patterns (made here by modulating each one) are
correlated against it. A frame is the preamble (8 zero symbols), the SFD 0xA7, a length byte, then the MAC frame
(MPDU) ending in a CRC-16, which must check. JSON lines out:
  {"event": "frame", "channel", "length", "type", "seq", "pan", "dst", "src", "src_ext", "dst_ext", "security",
   "ack_request", "protocol", "rssi", "hex", "t"}
"""

import argparse
import json
import sys
import time

import numpy as np

from ..receiver import dsp

CHIP_RATE = 2_000_000
SYMBOL0 = "11011001110000110101001000101110"         # c0 first
FRAME_TYPES = ("beacon", "data", "ack", "command", "reserved", "multipurpose", "fragment", "extended")


def chips(symbol: int) -> np.ndarray:
    """A symbol's 32 chips: symbols 1-7 shift symbol 0 right by 4 chips each; 8-15 are 0-7 with odd chips inverted."""
    base = np.array([int(c) for c in SYMBOL0], np.int8)
    c = np.roll(base, 4 * (symbol & 7))
    if symbol >= 8:
        c = c.copy()
        c[1::2] ^= 1
    return c


def crc16(data: bytes) -> int:
    """802.15.4's FCS: CRC-16 ITU-T, LSB first, initial 0 (the KERMIT variant)."""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c


def modulate(symbols, rate=4_000_000):
    """O-QPSK with half-sine pulses: I takes even chips, Q odd chips one chip later; complex baseband at *rate*."""
    sps = rate // CHIP_RATE
    c = np.concatenate([chips(s) for s in symbols]).astype(float) * 2 - 1
    n = len(c) * sps + 2 * sps
    i, q = np.zeros(n), np.zeros(n)
    pulse = np.sin(np.pi * np.arange(2 * sps) / (2 * sps))       # half-sine over two chips
    for k, v in enumerate(c):
        start = k * sps
        (i if k % 2 == 0 else q)[start:start + 2 * sps] += v * pulse
    return (i + 1j * q).astype(np.complex64)


def frame_symbols(mpdu_without_fcs: bytes):
    fcs = crc16(mpdu_without_fcs)
    psdu = mpdu_without_fcs + bytes([fcs & 0xFF, fcs >> 8])
    data = bytes(4) + bytes([0xA7, len(psdu)]) + psdu
    out = []
    for b in data:
        out += [b & 0xF, b >> 4]                                  # low nibble first
    return out


def _templates(sps):
    """Each symbol's discriminator signs (32 * sps values), modulated after a symbol 0 for context."""
    t = []
    for s in range(16):
        x = modulate([0, s, 0], CHIP_RATE * sps)
        d = np.angle(x[1:] * np.conj(x[:-1]))
        seg = d[32 * sps:64 * sps]
        t.append(np.sign(seg))
    return np.array(t, np.float32)


def parse_mac(m: bytes) -> dict:
    """The MAC header of an MPDU (without its FCS)."""
    out = {}
    if len(m) < 3:
        return out
    fc = m[0] | (m[1] << 8)
    out["type"] = FRAME_TYPES[fc & 7]
    out["security"] = bool(fc & 0x08)
    out["ack_request"] = bool(fc & 0x20)
    compress = bool(fc & 0x40)
    dmode, smode = (fc >> 10) & 3, (fc >> 14) & 3
    out["seq"] = m[2]
    i = 3

    def addr(mode):
        nonlocal i
        if mode == 2:
            v = f"0x{m[i] | (m[i + 1] << 8):04X}"
            i += 2
            return v, None
        if mode == 3:
            v = ":".join(f"{b:02X}" for b in reversed(m[i:i + 8]))
            i += 8
            return None, v
        return None, None

    try:
        if dmode:
            out["pan"] = f"0x{m[i] | (m[i + 1] << 8):04X}"
            i += 2
            out["dst"], out["dst_ext"] = addr(dmode)
        if smode:
            if not (compress and dmode):
                out["src_pan"] = f"0x{m[i] | (m[i + 1] << 8):04X}"
                out.setdefault("pan", out["src_pan"])
                i += 2
            out["src"], out["src_ext"] = addr(smode)
    except IndexError:
        return out
    payload = m[i:]
    out["protocol"] = ""
    if out["type"] == "beacon" and len(payload) >= 4:
        # superframe spec (2), GTS (1), pending (1), then the Zigbee beacon payload: protocol 0, profile, ext PAN ID
        p = payload[4:]
        if len(p) >= 15 and p[0] == 0:
            out["protocol"] = "Zigbee"
            out["ext_pan"] = ":".join(f"{b:02X}" for b in reversed(p[3:11]))
            out["permit_join"] = bool(payload[1] & 0x80)
    elif out["type"] == "data" and payload:
        d = payload[0]
        if d in (0x41, 0x60, 0x61, 0x62, 0x63) or (d & 0xE0) == 0x60 or (d & 0xC0) == 0x80:
            out["protocol"] = "Thread / 6LoWPAN"
        elif len(payload) >= 2 and (payload[0] & 0x03) in (0, 1) and ((payload[0] >> 2) & 0x0F) in (2, 3):
            out["protocol"] = "Zigbee"
    for k in [k for k, v in out.items() if v is None]:
        del out[k]
    return out


class Receiver:
    def __init__(self, rate):
        self.rate = rate
        self.sps = rate // CHIP_RATE
        self.tmpl = _templates(self.sps)
        self.L = 32 * self.sps
        self.buf = np.zeros(0, np.complex64)
        self.t0 = 0
        self.done_until = 0
        pre = np.tile(self.tmpl[0], 4)                            # 4 preamble symbols
        self.pre = pre / np.linalg.norm(pre)

    def feed(self, x):
        self.buf = np.concatenate([self.buf, x])
        out = []
        if len(self.buf) < self.L * 20:
            return out
        d = np.angle(self.buf[1:] * np.conj(self.buf[:-1]))
        # A frequency offset shows as a constant: the mean (spread chips are balanced; the median of MSK's two
        # values is one of them, and took it away).
        d = d - np.mean(d)
        s = np.sign(d).astype(np.float32)
        # Preamble candidates: where 4 zero symbols correlate well.
        n = len(s) - len(self.pre)
        if n > 0:
            corr = np.correlate(s[:n + len(self.pre) - 1], self.pre, mode="valid") / np.sqrt(len(self.pre))
            cand = np.nonzero(corr > 0.6)[0]                     # 1.0 is a perfect match
            last = -10 ** 9
            for p in cand:
                if p - last < self.L or self.t0 + p < self.done_until:
                    continue
                last = p
                f = self._frame(s, p)
                if f is not None:
                    out.append(f)
                    self.done_until = self.t0 + f.pop("_end")
                    last = f.pop("_rel_end", p)
        keep = self.L * 600                                       # two of the longest frames (127 bytes)
        if len(self.buf) > keep:
            cut = len(self.buf) - keep
            self.buf = self.buf[cut:]
            self.t0 += cut
        return out

    def _sym(self, s, at):
        seg = s[at:at + self.L]
        if len(seg) < self.L:
            return None, 0.0
        c = self.tmpl @ seg / self.L
        k = int(np.argmax(c))
        return k, float(c[k])

    def _frame(self, s, p):
        # Walk on from the preamble, symbol by symbol, to the SFD (7 then A), fine-tuning the timing.
        at = p
        for _ in range(12):
            best = max((self._sym(s, at + o) + (o,) for o in range(-self.sps, self.sps + 1)), key=lambda r: r[1])
            k, q, o = best
            if k is None:
                return None
            at += o
            if k == 7:
                k2, q2 = self._sym(s, at + self.L)
                if k2 == 0xA:
                    at += 2 * self.L
                    break
            elif k != 0:
                return None
            at += self.L
        else:
            return None
        syms = []
        need = 2
        while len(syms) < need:
            k, q = self._sym(s, at)
            if k is None:
                return None
            syms.append(k)
            at += self.L
            if len(syms) == 2:
                length = (syms[0] | (syms[1] << 4)) & 0x7F
                if length < 5:
                    return None
                need = 2 + 2 * length
        data = bytes(syms[2 + 2 * i] | (syms[3 + 2 * i] << 4) for i in range(length))
        if crc16(data[:-2]) != (data[-2] | (data[-1] << 8)):
            return None
        mpdu = data[:-2]
        seg = self.buf[p:at]
        rssi = 10 * np.log10(np.mean(np.abs(seg) ** 2) + 1e-20)
        f = parse_mac(mpdu)
        f.update(event="frame", length=length, hex=mpdu.hex(), rssi=round(float(rssi), 1), _end=at, _rel_end=at)
        return f


def channel_hz(ch: int) -> float:
    return (2405 + 5 * (ch - 11)) * 1e6


def emit(f):
    sys.stdout.write(json.dumps(f) + "\n")
    sys.stdout.flush()


def run_live(a):
    """Over rtl_tcp (the relay bridge to a HackRF): each channel for --dwell s, round and round."""
    import socket
    import struct

    def cmd(c, v):
        return struct.pack(">BI", c, int(v) & 0xFFFFFFFF)
    try:
        sock = socket.create_connection(("127.0.0.1", a.port), timeout=10)
    except OSError as e:
        sys.exit(f"zigbee: couldn't reach the SDR's rtl_tcp: {e}")
    head = b""
    while len(head) < 12:
        d = sock.recv(12 - len(head))
        if not d:
            sys.exit("zigbee: the SDR's rtl_tcp closed the connection")
        head += d
    sock.settimeout(10)
    chans = [int(c) for c in a.channels.split(",") if c.strip()]
    i = 0
    while True:
        ch = chans[i % len(chans)]
        f = channel_hz(ch)
        sock.sendall(cmd(2, a.rate) + cmd(0x40, int(f) >> 32) + cmd(1, int(f) & 0xFFFFFFFF))
        r = Receiver(a.rate)
        skip = int(0.05 * a.rate) * 2
        got = 0
        until = int(a.dwell * a.rate) * 2 if len(chans) > 1 else float("inf")
        pend = b""
        emit({"event": "channel", "channel": ch})
        while got < until:
            try:
                d = sock.recv(1 << 20)
            except TimeoutError:
                sys.exit("zigbee: no samples for 10 s: the SDR isn't streaming")
            if not d:
                sys.exit("zigbee: the SDR's rtl_tcp closed the connection")
            got += len(d)
            if skip > 0:
                cut = min(skip, len(d))
                skip -= cut
                d = d[cut:]
            d = pend + d
            n = len(d) // 2 * 2
            pend = d[n:]
            for fr in r.feed(dsp.cu8(d[:n])):
                fr.update(channel=ch, t=time.time())
                emit(fr)
        i += 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=4_000_000)
    ap.add_argument("--input")
    ap.add_argument("--port", type=int)
    ap.add_argument("--channels", default=",".join(str(c) for c in range(11, 27)))
    ap.add_argument("--dwell", type=float, default=3.0)
    ap.add_argument("--channel", type=int, default=0)
    a = ap.parse_args(argv)
    if a.rate % CHIP_RATE:
        sys.exit("zigbee: the sample rate must be a multiple of 2 MS/s")
    if a.port:
        run_live(a)
        return
    r = Receiver(a.rate)
    src = sys.stdin.buffer if not a.input else open(a.input, "rb")
    while True:
        data = src.read(1 << 20)
        if not data or len(data) < 2:
            break
        for f in r.feed(dsp.cu8(data[:len(data) // 2 * 2])):
            f.update(channel=a.channel or None, t=time.time())
            emit(f)
    for f in r.feed(np.zeros(r.L * 40, np.complex64)):
        f.update(channel=a.channel or None, t=time.time())
        emit(f)


if __name__ == "__main__":
    main()
