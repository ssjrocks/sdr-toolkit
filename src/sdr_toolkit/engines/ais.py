"""AIS: !AIVDM sentences in, ship reports out.

Message layouts as in ITU-R M.1371 (bit offsets from the start of the payload):
  1/2/3  class A position     5   static and voyage data (two sentences)
  18     class B position     19  class B extended position (with name)
  24     class B static data (part A: name; part B: type and callsign)
"""

from dataclasses import dataclass, field

SIXBIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
NAV_STATUS = ("under way using engine", "at anchor", "not under command", "restricted manoeuvrability",
              "constrained by draught", "moored", "aground", "engaged in fishing", "under way sailing",
              "", "", "power-driven vessel towing astern", "power-driven vessel pushing ahead", "",
              "AIS-SART active", "")


def ship_type(code: int) -> str:
    named = {30: "fishing", 31: "towing", 32: "towing (large)", 33: "dredging", 34: "diving",
             35: "military", 36: "sailing", 37: "pleasure craft", 50: "pilot", 51: "search and rescue",
             52: "tug", 53: "port tender", 55: "law enforcement", 58: "medical"}
    if code in named:
        return named[code]
    for lo, name in ((40, "high-speed craft"), (60, "passenger"), (70, "cargo"), (80, "tanker"), (90, "other")):
        if lo <= code < lo + 10:
            return name
    return ""


def checksum_ok(sentence: str) -> bool:
    if "*" not in sentence:
        return False
    body, cs = sentence[1:].split("*", 1)
    x = 0
    for c in body:
        x ^= ord(c)
    return cs[:2].upper() == f"{x:02X}"


def payload_bits(payload: str, fill: int) -> str:
    bits = []
    for c in payload:
        v = ord(c) - 48
        if v > 40:
            v -= 8
        bits.append(f"{v & 0x3F:06b}")
    s = "".join(bits)
    return s[: len(s) - fill] if fill else s


class Bits:
    def __init__(self, s: str):
        self.s = s

    def u(self, a, n):
        part = self.s[a : a + n]
        return int(part, 2) if len(part) == n else None

    def i(self, a, n):
        v = self.u(a, n)
        if v is None:
            return None
        return v - (1 << n) if v & (1 << (n - 1)) else v

    def text(self, a, n):
        out = []
        for k in range(a, a + n, 6):
            v = self.u(k, 6)
            if v is None:
                break
            out.append(SIXBIT[v])
        return "".join(out).replace("@", " ").strip()


@dataclass
class Report:
    kind: int
    mmsi: int
    fields: dict = field(default_factory=dict)


def _position(b: Bits, lon_at, lat_at, sog_at, cog_at, hdg_at) -> dict:
    d = {}
    lon, lat = b.i(lon_at, 28), b.i(lat_at, 27)
    if lon is not None and lat is not None and abs(lon) != 181 * 600000 and abs(lat) != 91 * 600000:
        d["lon"], d["lat"] = lon / 600000, lat / 600000
    sog = b.u(sog_at, 10)
    if sog is not None and sog != 1023:
        d["speed"] = sog / 10
    cog = b.u(cog_at, 12)
    if cog is not None and cog != 3600:
        d["course"] = cog / 10
    hdg = b.u(hdg_at, 9)
    if hdg is not None and hdg != 511:
        d["heading"] = hdg
    return d


def decode_payload(bits: str) -> Report | None:
    b = Bits(bits)
    kind, mmsi = b.u(0, 6), b.u(8, 30)
    if kind is None or mmsi is None:
        return None
    d = {}
    if kind in (1, 2, 3):
        d = _position(b, 61, 89, 50, 116, 128)
        st = b.u(38, 4)
        if st is not None and NAV_STATUS[st]:
            d["status"] = NAV_STATUS[st]
        d["class"] = "A"
    elif kind == 18:
        d = _position(b, 57, 85, 46, 112, 124)
        d["class"] = "B"
    elif kind == 19:
        d = _position(b, 57, 85, 46, 112, 124)
        d.update({"name": b.text(143, 120), "type": ship_type(b.u(263, 8) or 0), "class": "B"})
    elif kind == 5:
        d = {"callsign": b.text(70, 42), "name": b.text(112, 120), "type": ship_type(b.u(232, 8) or 0),
             "destination": b.text(302, 120), "class": "A"}
    elif kind == 24:
        part = b.u(38, 2)
        if part == 0:
            d = {"name": b.text(40, 120)}
        elif part == 1:
            d = {"type": ship_type(b.u(40, 8) or 0), "callsign": b.text(90, 42)}
        d["class"] = "B"
    else:
        return Report(kind, mmsi, {})
    return Report(kind, mmsi, {k: v for k, v in d.items() if v not in ("", None)})


class Assembler:
    """Collects !AIVDM sentences, joining multi-sentence messages; returns reports."""

    def __init__(self):
        self._parts: dict[tuple, list] = {}

    def add(self, sentence: str) -> Report | None:
        sentence = sentence.strip()
        if not sentence.startswith(("!AIVDM", "!AIVDO")) or not checksum_ok(sentence):
            return None
        f = sentence.split("*")[0].split(",")
        if len(f) < 7:
            return None
        try:
            count, num, fill = int(f[1]), int(f[2]), int(f[6] or 0)
        except ValueError:
            return None
        if count == 1:
            return decode_payload(payload_bits(f[5], fill))
        key = (f[3], f[4], count)
        parts = self._parts.setdefault(key, [None] * count)
        if not 1 <= num <= count:
            return None
        parts[num - 1] = (f[5], fill)
        if any(p is None for p in parts):
            return None
        del self._parts[key]
        bits = "".join(payload_bits(p, fl) for p, fl in parts)
        return decode_payload(bits)


def encode_sentences(bits: str, channel="A", seq="1") -> list[str]:
    """!AIVDM sentences for a payload (for tests): 60 characters per sentence."""
    fill = -len(bits) % 6
    bits += "0" * fill
    chars = []
    for k in range(0, len(bits), 6):
        v = int(bits[k : k + 6], 2)
        chars.append(chr(v + 48 if v < 40 else v + 56))
    payload = "".join(chars)
    pieces = [payload[i : i + 60] for i in range(0, len(payload), 60)]
    out = []
    for n, piece in enumerate(pieces, 1):
        body = f"AIVDM,{len(pieces)},{n},{seq if len(pieces) > 1 else ''},{channel},{piece},{fill if n == len(pieces) else 0}"
        x = 0
        for c in body:
            x ^= ord(c)
        out.append(f"!{body}*{x:02X}")
    return out
