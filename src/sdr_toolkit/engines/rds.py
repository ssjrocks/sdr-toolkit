"""RDS (Radio Data System, IEC 62106 / EN 50067): the data a broadcast FM station sends on a 57 kHz subcarrier of
its multiplex: its name (PS), radiotext (RT), programme type (PTY), traffic flags (TP / TA), music / speech,
alternative frequencies (AF), the clock (CT) and its programme identification code (PI).

The multiplex (the FM discriminator's output before de-emphasis) is mixed down from 57 kHz and low-passed. RDS is
BPSK of biphase symbols: 1187.5 bit/s, each bit a +- chip pair, differentially coded. The carrier's phase comes
from squaring (BPSK's two phases become one), the bit clock from an early-late gate on the biphase symbols. Bits
form 26-bit blocks, 16 of data and a 10-bit check word with an offset (A, B, C, C', D) that marks the block's
place in its 4-block group; a block counts only when its check word is right.
"""

import time

import numpy as np
from scipy.signal import firwin, lfilter

BIT_RATE = 1187.5
RDS_RATE = 19000                    # 16 samples a bit
POLY = 0x5B9                        # x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1
OFFSETS = {"A": 0x0FC, "B": 0x198, "C": 0x168, "C'": 0x350, "D": 0x1B4}
ORDER = ("A", "B", "C", "D")

# Programme types: RDS (Europe, Australia...) and RBDS (North America) name them differently.
PTY_RDS = ("", "News", "Current affairs", "Information", "Sport", "Education", "Drama", "Culture", "Science",
           "Varied", "Pop music", "Rock music", "Easy listening", "Light classical", "Serious classical",
           "Other music", "Weather", "Finance", "Children's", "Social affairs", "Religion", "Phone-in", "Travel",
           "Leisure", "Jazz", "Country", "National music", "Oldies", "Folk", "Documentary", "Alarm test", "Alarm")
PTY_RBDS = ("", "News", "Information", "Sports", "Talk", "Rock", "Classic rock", "Adult hits", "Soft rock",
            "Top 40", "Country", "Oldies", "Soft", "Nostalgia", "Jazz", "Classical", "Rhythm and blues",
            "Soft rhythm and blues", "Language", "Religious music", "Religious talk", "Personality", "Public",
            "College", "", "", "", "", "", "Weather", "Emergency test", "Emergency")


def checkword(data: int) -> int:
    """The 10-bit CRC of 16 data bits (before the offset)."""
    reg = data << 10
    for i in range(25, 9, -1):
        if reg & (1 << i):
            reg ^= POLY << (i - 10)
    return reg & 0x3FF


def block_kind(block26: int) -> str | None:
    """Which offset a received 26-bit block carries, or None if its check word is wrong."""
    syn = checkword(block26 >> 10) ^ (block26 & 0x3FF)
    for k, off in OFFSETS.items():
        if syn == off:
            return k
    return None


def _burst_table(max_len=2) -> dict[int, int]:
    """Syndrome -> error pattern, for every burst of up to max_len bits in a 26-bit block."""
    table = {}
    for length in range(1, max_len + 1):
        for burst in range(1 << (length - 1), 1 << length):
            if not burst & 1 and length > 1:
                continue
            for shift in range(0, 27 - length):
                e = burst << shift
                syn = checkword(e >> 10) ^ (e & 0x3FF)
                table.setdefault(syn, e)
    return table


BURSTS = _burst_table()


def corrected(block26: int, kind: str) -> int | None:
    """The block with a burst of up to 2 bit errors fixed, if the offset it should carry makes that possible."""
    syn = checkword(block26 >> 10) ^ (block26 & 0x3FF) ^ OFFSETS[kind]
    e = BURSTS.get(syn)
    return None if e is None else block26 ^ e


def encode_block(data: int, kind: str) -> int:
    return (data << 10) | (checkword(data) ^ OFFSETS[kind])


def rds_char(c: int) -> str:
    """RDS's character set: ASCII for the printable range, a few accented letters above."""
    if 32 <= c < 127:
        return chr(c)
    extra = {0x80: "á", 0x81: "à", 0x82: "é", 0x83: "è", 0x84: "í", 0x85: "ì", 0x86: "ó", 0x87: "ò", 0x88: "ú",
             0x89: "ù", 0x8A: "Ñ", 0x8B: "Ç", 0x8C: "Ş", 0x8D: "ß", 0x8E: "¡", 0x91: "ä", 0x97: "ö", 0x99: "ü",
             0xD1: "Ä", 0xD7: "Ö", 0xD9: "Ü"}
    return extra.get(c, " ")


def af_mhz(code: int) -> float | None:
    """An alternative-frequency code (1-204: 87.6-107.9 MHz)."""
    return round(87.5 + code / 10, 1) if 1 <= code <= 204 else None


class Station:
    """What one station's groups have said so far."""

    def __init__(self):
        self.pi = None
        self._pi_count: dict[int, int] = {}
        self._pty_count: dict[int, int] = {}
        self.ps = [" "] * 8
        self.ps_seen = [False] * 4
        self.rt = [" "] * 64
        self.rt_ab = None
        self.rt_seen = set()
        self.ptyn = [" "] * 8
        self.pty = None
        self.tp = self.ta = self.ms = None
        self.stereo = None
        self.af: set[float] = set()
        self.clock = None
        self.groups = 0
        self.group_types: dict[str, int] = {}
        self.ps_done = ""
        self.rt_done = ""

    def apply(self, a, b, c, d, c_kind="C"):
        """One group's blocks (None where a block failed its check)."""
        if a is not None:
            self._pi_count[a] = self._pi_count.get(a, 0) + 1
            best = max(self._pi_count, key=self._pi_count.get)
            if self._pi_count[best] >= 2:
                self.pi = best
        if b is None:
            return []
        self.groups += 1
        gtype, version = (b >> 12) & 0xF, (b >> 11) & 1
        name = f"{gtype}{'AB'[version]}"
        self.group_types[name] = self.group_types.get(name, 0) + 1
        self.tp = bool(b & 0x400)
        # The programme type the station sends most (one bad block can't change it).
        p = (b >> 5) & 0x1F
        self._pty_count[p] = self._pty_count.get(p, 0) + 1
        self.pty = max(self._pty_count, key=self._pty_count.get)
        if version == 1 and c is not None and c_kind == "C'":
            self._pi_count[c] = self._pi_count.get(c, 0) + 1
        changed = []
        if gtype == 0:
            seg = b & 3
            self.ta, self.ms = bool(b & 0x10), bool(b & 0x8)
            if seg == 0:
                self.stereo = bool(b & 0x4)              # DI bit d3, sent in segment 0
            if d is not None:
                self.ps[2 * seg:2 * seg + 2] = [rds_char(d >> 8), rds_char(d & 0xFF)]
                self.ps_seen[seg] = True
                if all(self.ps_seen):
                    ps = "".join(self.ps)
                    if ps != self.ps_done:
                        self.ps_done = ps
                        changed.append("ps")
                    self.ps_seen = [False] * 4
            if version == 0 and c is not None:
                for code in (c >> 8, c & 0xFF):
                    f = af_mhz(code)
                    if f:
                        self.af.add(f)
        elif gtype == 2:
            ab = (b >> 4) & 1
            if self.rt_ab is not None and ab != self.rt_ab:
                self.rt = [" "] * 64                     # a new text
                self.rt_seen = set()
            self.rt_ab = ab
            seg = b & 0xF
            if version == 0:
                chars = ([c >> 8, c & 0xFF] if c is not None else [None, None]) + \
                        ([d >> 8, d & 0xFF] if d is not None else [None, None])
                pos = 4 * seg
            else:
                chars = [d >> 8, d & 0xFF] if d is not None else [None, None]
                pos = 2 * seg
            for i, ch in enumerate(chars):
                if ch is not None and pos + i < 64:
                    self.rt[pos + i] = "" if ch == 0x0D else rds_char(ch)
            if c is not None or version == 1:
                self.rt_seen.add(seg)
            text = self.radiotext()
            # The text is complete once its segments up to the carriage return have come (or all 16); a
            # station that pads with spaces instead: once every segment with text in it has come, and one
            # of the blank ones after.
            per = 4 if version == 0 else 2
            end = next((i for i, x in enumerate(self.rt) if x == ""), 64)
            need = set(range((end + per - 1) // per))
            if not need <= self.rt_seen and end == 64 and text:
                last = max(i for i, x in enumerate(self.rt) if x not in (" ", ""))
                with_text = set(range(last // per + 1))
                if with_text <= self.rt_seen and any(k > last // per for k in self.rt_seen):
                    need = with_text
            if need <= self.rt_seen and text and text != self.rt_done:
                self.rt_done = text
                changed.append("rt")
        elif gtype == 4 and version == 0 and c is not None and d is not None:
            mjd = ((b & 3) << 15) | (c >> 1)
            hour = ((c & 1) << 4) | (d >> 12)
            minute = (d >> 6) & 0x3F
            offset = (d & 0x1F) * 30 * (-1 if d & 0x20 else 1)       # minutes
            y = int((mjd - 15078.2) / 365.25)
            m = int((mjd - 14956.1 - int(y * 365.25)) / 30.6001)
            day = mjd - 14956 - int(y * 365.25) - int(m * 30.6001)
            k = 1 if m in (14, 15) else 0
            year, month = 1900 + y + k, m - 1 - k * 12
            if 1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60:
                total = hour * 60 + minute + offset
                lh, lm = (total // 60) % 24, total % 60
                self.clock = (f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d} UTC "
                              f"({lh:02d}:{lm:02d} local, UTC{'+' if offset >= 0 else '-'}{abs(offset) / 60:g})")
                changed.append("clock")
        elif gtype == 10 and version == 0 and c is not None and d is not None:
            seg = b & 1
            self.ptyn[4 * seg:4 * seg + 4] = [rds_char(x) for x in (c >> 8, c & 0xFF, d >> 8, d & 0xFF)]
        return changed

    def radiotext(self) -> str:
        out = []
        for x in self.rt:
            if x == "":
                break
            out.append(x)
        return "".join(out).strip()

    def summary(self, rbds=False) -> dict:
        names = PTY_RBDS if rbds else PTY_RDS
        return {"pi": f"{self.pi:04X}" if self.pi is not None else "", "ps": self.ps_done.strip(),
                "rt": self.rt_done, "pty": names[self.pty] if self.pty is not None else "",
                "ptyn": "".join(self.ptyn).strip(), "tp": self.tp, "ta": self.ta,
                "music": self.ms, "stereo": self.stereo, "af": sorted(self.af), "clock": self.clock,
                "groups": self.groups, "group_types": dict(self.group_types)}


class Decoder:
    """Multiplex samples in, RDS groups out (into a Station). Keeps its state between calls."""

    def __init__(self, mpx_rate: int):
        self.rate = int(mpx_rate)
        self.lp = firwin(255, 2400, fs=self.rate)
        self.lp_z = np.zeros(len(self.lp) - 1, complex)
        self.n = 0                                       # samples of multiplex so far (the mixer's phase)
        self.base = np.zeros(0, complex)                 # the subcarrier at 0 Hz, 19 kHz, not yet used
        self._tail = np.zeros(1, complex)
        self._pos = 0.0
        self._sq = 0j
        self.theta = 0.0
        self.t = 0.0                                     # where the next bit starts, in self.base samples
        self.prev_bit = 0
        self.bits = 0                                    # the last 26 bits
        self.nbits = 0
        self.synced = False
        self.confirmed = False
        self.good_run = 0
        self.expect = 0                                  # which block of the group comes next
        self.since_sync = 0
        self.bad = 0
        self.group = [None] * 4
        self.c_kind = "C"
        self.station = Station()
        self.blocks_ok = 0
        self.blocks_bad = 0
        self.blocks_fixed = 0

    def feed(self, mpx: np.ndarray) -> list[str]:
        """Multiplex samples (the discriminator, any scale) -> what changed ("ps", "rt", "clock")."""
        x = np.asarray(mpx, np.float64)
        if not len(x):
            return []
        k = self.n + np.arange(len(x))
        self.n += len(x)
        z = x * np.exp(-2j * np.pi * 57000.0 * k / self.rate)
        z, self.lp_z = lfilter(self.lp, 1.0, z, zi=self.lp_z)
        # To 19 kHz by interpolation, carrying the position over from one call to the next (a stateless
        # resampler glitches at every block edge, and a receiver's blocks can be short).
        buf = np.concatenate([self._tail, z])
        step = self.rate / RDS_RATE
        pos = np.arange(self._pos, len(buf) - 1, step)
        i = pos.astype(int)
        f = pos - i
        y = buf[i] * (1 - f) + buf[i + 1] * f
        self._pos = (pos[-1] + step if len(pos) else self._pos) - (len(buf) - 1)
        self._tail = buf[-1:]
        # The carrier's phase, from squaring (two BPSK phases -> one), averaged over about 50 ms whatever
        # the block size.
        if len(y):
            a = min(1.0, len(y) / (0.05 * RDS_RATE))
            self._sq = (1 - a) * self._sq + a * np.mean(y ** 2)
            est = np.angle(self._sq) / 2
            # Squaring leaves a 180 degree ambiguity: stay near the last estimate (the differential code
            # doesn't care which of the two it is).
            if abs(np.angle(np.exp(1j * (est - self.theta)))) > np.pi / 2:
                est += np.pi
            self.theta = float(np.angle(np.exp(1j * est)))
        soft = np.real(y * np.exp(-1j * self.theta))
        self.base = np.concatenate([self.base, soft])
        return self._bits()

    def _bits(self):
        spb = RDS_RATE / BIT_RATE                        # 16
        c = np.concatenate([[0.0], np.cumsum(self.base)])

        def area(a, b):                                  # the integral of base over [a, b), fractional ends
            def at(p):
                i = int(np.floor(p))
                if i + 1 >= len(c):
                    i = len(c) - 2
                return c[i] + (c[i + 1] - c[i]) * (p - i)
            return at(b) - at(a)

        changed = []
        while self.t + spb + 2 < len(self.base):
            t, h = self.t, spb / 2
            sym = area(t, t + h) - area(t + h, t + spb)
            d = 1.0
            early = abs(area(t - d, t - d + h) - area(t - d + h, t - d + spb)) if t >= d else abs(sym)
            late = abs(area(t + d, t + d + h) - area(t + d + h, t + d + spb))
            # Early-late gate, normalised so its step stays within a fraction of a sample (noise gives a
            # symbol near zero, which unnormalised would throw the clock anywhere).
            self.t += spb + 0.3 * (late - early) / (late + early + 1e-9)
            bit = 1 if sym > 0 else 0
            data = bit ^ self.prev_bit
            self.prev_bit = bit
            changed += self._bit(data)
        drop = int(self.t) - 4
        if drop > 0:
            self.base = self.base[drop:]
            self.t -= drop
        return changed

    def _bit(self, b):
        self.bits = ((self.bits << 1) | b) & ((1 << 26) - 1)
        self.nbits += 1
        if not self.synced:
            if self.nbits < 26:
                return []
            kind = block_kind(self.bits)
            if kind is None:
                return []
            self.synced = True
            self.confirmed = False                       # until a second block turns up in its place
            self.expect = {"A": 1, "B": 2, "C": 3, "C'": 3, "D": 0}[kind]
            self.group = [None] * 4
            self._store(kind)
            self.since_sync = 0
            self.bad = 0
            self.good_run = 1
            return []
        self.since_sync += 1
        if self.since_sync < 26:
            return []
        self.since_sync = 0
        kind = block_kind(self.bits)
        want = ORDER[self.expect]
        ok = kind == want or (want == "C" and kind == "C'")
        changed = []
        if not self.confirmed:
            if not ok:                                   # the first block was chance: look again
                self.synced = False
                self.nbits = 0
                return []
            self.confirmed = True
        # A short burst of errors: fixed when the offset this place in the group should carry explains it,
        # and only while the blocks around it are mostly good (in noise a "fix" would be made-up data).
        if not ok and self.bad == 0 and self.good_run >= 4:
            for k in (want, "C'") if want == "C" else (want,):
                fixed = corrected(self.bits, k)
                if fixed is not None:
                    self.bits, kind, ok = fixed, k, True
                    self.blocks_fixed += 1
                    break
        if ok:
            self.blocks_ok += 1
            self.bad = 0
            self.good_run += 1
            self._store(kind)
        else:
            self.blocks_bad += 1
            self.bad += 1
            self.good_run = 0
            if self.bad > 20:                            # lost: look for a block anywhere again
                self.synced = False
                self.nbits = 0
                return []
        if self.expect == 3:
            a, b, c, d = self.group
            changed = self.station.apply(a, b, c, d, self.c_kind)
            self.group = [None] * 4
            self.c_kind = "C"
        self.expect = (self.expect + 1) % 4
        return changed

    def _store(self, kind):
        i = {"A": 0, "B": 1, "C": 2, "C'": 2, "D": 3}[kind]
        self.group[i] = self.bits >> 10
        if kind in ("C", "C'"):
            self.c_kind = kind


# ---- a test signal ---------------------------------------------------------------------------------


def groups_for(pi=0x9B12, ps="TESTFM  ", rt="Now playing: a generated RDS test signal", pty=10, tp=True):
    """0A groups for the name and 2A for the radiotext, as a station sends them (data words per group)."""
    out = []
    b0 = (0 << 12) | (0 << 11) | (int(tp) << 10) | (pty << 5)
    for seg in range(4):
        out.append((pi, b0 | 0x8 | seg | (0x4 if seg == 0 else 0), (0xE0 << 8) | 0xCD,
                    (ord(ps[2 * seg]) << 8) | ord(ps[2 * seg + 1])))
    text = (rt + "\r").ljust(4 * ((len(rt) + 4) // 4))[:64]
    for seg in range(len(text) // 4):
        q = text[4 * seg:4 * seg + 4]
        out.append((pi, (2 << 12) | (int(tp) << 10) | (pty << 5) | seg,
                    (ord(q[0]) << 8) | ord(q[1]), (ord(q[2]) << 8) | ord(q[3])))
    return out


def modulate(groups, rate=240000, repeats=6, level=0.05, pilot=0.09) -> np.ndarray:
    """A multiplex (as the discriminator gives it, +-1 = 75 kHz) with a 19 kHz pilot and the RDS of *groups*."""
    bits = []
    for _ in range(repeats):
        for g in groups:
            for word, kind in zip(g, ORDER):
                blk = encode_block(word, kind)
                bits += [(blk >> (25 - i)) & 1 for i in range(26)]
    # Differential coding, then biphase: a 1 is +-, a 0 is -+.
    enc, prev = [], 0
    for b in bits:
        prev ^= b
        enc.append(prev)
    spb = rate / BIT_RATE
    n = int(len(enc) * spb)
    t = np.arange(n) / rate
    idx = np.minimum((t * BIT_RATE).astype(int), len(enc) - 1)
    frac = t * BIT_RATE - np.floor(t * BIT_RATE)
    sym = np.where(np.array(enc)[idx] == 1, 1.0, -1.0) * np.where(frac < 0.5, 1.0, -1.0)
    from scipy.signal import firwin as _fw
    sym = np.convolve(sym, _fw(101, 2400, fs=rate), mode="same")
    return pilot * np.sin(2 * np.pi * 19000 * t) + level * sym * np.sin(2 * np.pi * 57000 * t)


def now() -> float:
    return time.time()
