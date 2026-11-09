"""Meshtastic and MeshCore packets, from the bytes of a LoRa frame.

Formats as published (read 2026-11-09):
  Meshtastic: firmware src/mesh/RadioInterface.h (16-byte header), CryptoEngine.cpp
    (AES-CTR, nonce = packet id as uint64 LE + sender as uint32 LE + 4 zero bytes),
    Channels.h/.cpp (default key, channel hash), protobufs mesh.proto / portnums.proto.
  MeshCore: docs/packet_format.md and docs/payloads.md, src/Utils.cpp (AES-128 in ECB
    blocks, then a 2-byte HMAC-SHA256 over the ciphertext, stored first), docs/faq.md
    (the Public channel key).
"""

import base64
import hashlib
import hmac
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---- protobuf (just enough to read Meshtastic's messages) --------------------


def pb_fields(buf: bytes) -> list[tuple[int, int, object]]:
    """(field number, wire type, value) of each field; raises ValueError on bad data."""
    out, i = [], 0

    def varint():
        nonlocal i
        v = shift = 0
        while True:
            if i >= len(buf):
                raise ValueError("truncated varint")
            b = buf[i]
            i += 1
            v |= (b & 0x7F) << shift
            if not b & 0x80:
                return v
            shift += 7
            if shift > 63:
                raise ValueError("varint too long")

    while i < len(buf):
        key = varint()
        num, wt = key >> 3, key & 7
        if num == 0:
            raise ValueError("field 0")
        if wt == 0:
            val = varint()
        elif wt == 1:
            val = buf[i : i + 8]
            i += 8
        elif wt == 2:
            n = varint()
            val = buf[i : i + n]
            if len(val) != n:
                raise ValueError("truncated bytes")
            i += n
        elif wt == 5:
            val = buf[i : i + 4]
            i += 4
        else:
            raise ValueError(f"wire type {wt}")
        if wt in (1, 5) and len(val) != (8 if wt == 1 else 4):
            raise ValueError("truncated fixed")
        out.append((num, wt, val))
    return out


def pb_dict(buf: bytes) -> dict:
    return {num: val for num, _, val in pb_fields(buf)}


def pb_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def pb_encode(fields: list[tuple[int, object]]) -> bytes:
    """Encode (number, value): int -> varint, bytes/str -> length-delimited, ('f32', int) -> sfixed32."""
    out = bytearray()
    for num, val in fields:
        if isinstance(val, tuple) and val[0] == "f32":
            out += pb_varint(num << 3 | 5) + struct.pack("<i", val[1])
        elif isinstance(val, int):
            out += pb_varint(num << 3) + pb_varint(val & ((1 << 64) - 1))
        else:
            b = val.encode() if isinstance(val, str) else bytes(val)
            out += pb_varint(num << 3 | 2) + pb_varint(len(b)) + b
    return bytes(out)


def _signed32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


# ---- Meshtastic --------------------------------------------------------------

DEFAULT_PSK = bytes([0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59,
                     0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01])
PORTS = {0: "unknown", 1: "text", 2: "remote hardware", 3: "position", 4: "node info", 5: "routing",
         6: "admin", 7: "compressed text", 8: "waypoint", 9: "audio", 67: "telemetry",
         70: "traceroute", 71: "neighbour info", 256: "private"}


def meshtastic_key(psk_b64: str) -> bytes:
    """A channel PSK as configured: base64 of 16/32 bytes, or of 1 byte (index into the default key)."""
    raw = base64.b64decode(psk_b64.strip() or "AQ==")
    if len(raw) == 1:
        if raw[0] == 0:
            return b""                      # no encryption
        k = bytearray(DEFAULT_PSK)
        k[-1] = (k[-1] + raw[0] - 1) & 0xFF
        return bytes(k)
    if len(raw) not in (16, 32):
        raise ValueError("a Meshtastic key is 1, 16 or 32 bytes")
    return raw


def channel_hash(name: str, key: bytes) -> int:
    h = 0
    for b in name.encode():
        h ^= b
    for b in key:
        h ^= b
    return h


def meshtastic_crypt(key: bytes, packet_id: int, sender: int, data: bytes) -> bytes:
    """AES-CTR; the same operation encrypts and decrypts."""
    if not key:
        return data
    nonce = struct.pack("<QI", packet_id, sender) + bytes(4)
    c = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    return c.update(data) + c.finalize()


@dataclass
class MeshtasticPacket:
    dest: int
    sender: int
    packet_id: int
    hop_limit: int
    hop_start: int
    want_ack: bool
    via_mqtt: bool
    channel: int
    next_hop: int
    relay: int
    payload: bytes
    decoded: dict = field(default_factory=dict)     # port, text / fields, if decrypted


def parse_meshtastic(frame: bytes, keys: list[tuple[str, bytes]]) -> MeshtasticPacket | None:
    """Header, then try each (channel name, key): the hash narrows it, the protobuf confirms it."""
    if len(frame) < 16:
        return None
    dest, sender, pid, flags, chan, nxt, relay = struct.unpack("<IIIBBBB", frame[:16])
    p = MeshtasticPacket(dest, sender, pid, flags & 7, flags >> 5, bool(flags & 8), bool(flags & 0x10),
                         chan, nxt, relay, frame[16:])
    candidates = sorted(keys, key=lambda nk: channel_hash(*nk) != chan)
    for name, key in candidates:
        plain = meshtastic_crypt(key, pid, sender, p.payload)
        d = decode_data(plain)
        if d is not None:
            d["channel"] = name
            p.decoded = d
            break
    return p


def decode_data(plain: bytes) -> dict | None:
    """A Meshtastic Data message, or None if this isn't one (wrong key)."""
    try:
        f = pb_dict(plain)
    except ValueError:
        return None
    port = f.get(1)
    if not isinstance(port, int) or port > 511 or not isinstance(f.get(2, b""), (bytes, bytearray)):
        return None
    payload = bytes(f.get(2, b""))
    d = {"port": PORTS.get(port, str(port))}
    try:
        if port == 1:
            d["text"] = payload.decode("utf-8")
        elif port == 3:
            pf = pb_dict(payload)
            if 1 in pf and 2 in pf:
                d["lat"] = struct.unpack("<i", pf[1])[0] * 1e-7
                d["lon"] = struct.unpack("<i", pf[2])[0] * 1e-7
            if 3 in pf:
                d["alt"] = _signed32(pf[3])
        elif port == 4:
            uf = pb_dict(payload)
            for num, key in ((1, "id"), (2, "long_name"), (3, "short_name")):
                if num in uf:
                    d[key] = bytes(uf[num]).decode("utf-8", "replace")
            if 5 in uf:
                d["hw_model"] = uf[5]
        else:
            d["bytes"] = len(payload)
    except (ValueError, UnicodeDecodeError, struct.error):
        return None
    return d


def describe_meshtastic(p: MeshtasticPacket) -> str:
    to = "everyone" if p.dest == 0xFFFFFFFF else f"!{p.dest:08x}"
    head = f"!{p.sender:08x} → {to}"
    hops = f"hops {p.hop_start - p.hop_limit}/{p.hop_start}" if p.hop_start else f"hop limit {p.hop_limit}"
    d = p.decoded
    if not d:
        return f"{head}: encrypted ({len(p.payload)} bytes, channel hash 0x{p.channel:02x}), {hops}"
    port = d["port"]
    if "text" in d:
        body = f'"{d["text"]}"'
    elif "lat" in d:
        body = f"position {d['lat']:.5f}, {d['lon']:.5f}" + (f", {d['alt']} m" if "alt" in d else "")
    elif "long_name" in d:
        body = f"node {d.get('long_name', '')} ({d.get('short_name', '')}, {d.get('id', '')})"
    else:
        body = f"{port}, {d.get('bytes', 0)} bytes"
    return f"{head} [{d['channel']}]: {body}, {hops}"


def build_meshtastic(sender, packet_id, data: bytes, key: bytes, channel: int, dest=0xFFFFFFFF,
                     hop_limit=3, hop_start=3) -> bytes:
    """A frame as a node sends it (for tests)."""
    flags = (hop_limit & 7) | (hop_start & 7) << 5
    head = struct.pack("<IIIBBBB", dest, sender, packet_id, flags, channel, 0, 0)
    return head + meshtastic_crypt(key, packet_id, sender, data)


# ---- MeshCore ----------------------------------------------------------------

PUBLIC_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")
ROUTES = {0: "transport flood", 1: "flood", 2: "direct", 3: "transport direct"}
TYPES = {0: "request", 1: "response", 2: "text message", 3: "ack", 4: "advert", 5: "group text",
         6: "group data", 7: "anonymous request", 8: "path", 9: "trace", 10: "multipart",
         11: "control", 15: "raw"}
ADVERT_KINDS = {1: "chat node", 2: "repeater", 3: "room server", 4: "sensor"}


def meshcore_key(text: str) -> bytes:
    """A channel key as the apps show it: 32 hex digits, or base64."""
    t = text.strip()
    try:
        k = bytes.fromhex(t)
    except ValueError:
        k = base64.b64decode(t)
    if len(k) not in (16, 32):
        raise ValueError("a MeshCore channel key is 16 or 32 bytes")
    return k


def meshcore_channel_hash(key: bytes) -> int:
    return hashlib.sha256(key[:16] if len(key) == 32 and not any(key[16:]) else key).digest()[0]


def _secret(key: bytes) -> bytes:
    return key + bytes(32 - len(key))        # kept as 32 bytes, zero-filled


def meshcore_decrypt(key: bytes, mac_and_cipher: bytes) -> bytes | None:
    mac, ct = mac_and_cipher[:2], mac_and_cipher[2:]
    if not ct or len(ct) % 16:
        return None
    secret = _secret(key)
    if hmac.new(secret, ct, hashlib.sha256).digest()[:2] != mac:
        return None
    d = Cipher(algorithms.AES(secret[:16]), modes.ECB()).decryptor()
    return d.update(ct) + d.finalize()


def meshcore_encrypt(key: bytes, plain: bytes) -> bytes:
    secret = _secret(key)
    plain = plain + bytes(-len(plain) % 16)
    e = Cipher(algorithms.AES(secret[:16]), modes.ECB()).encryptor()
    ct = e.update(plain) + e.finalize()
    return hmac.new(secret, ct, hashlib.sha256).digest()[:2] + ct


@dataclass
class MeshCorePacket:
    route: str
    kind: str
    version: int
    path: bytes
    payload: bytes
    decoded: dict = field(default_factory=dict)


def parse_meshcore(frame: bytes, keys: list[tuple[str, bytes]]) -> MeshCorePacket | None:
    if len(frame) < 2:
        return None
    h = frame[0]
    route, ptype, ver = h & 3, (h >> 2) & 0xF, h >> 6
    i = 1
    if route in (0, 3):
        i += 4                                   # transport codes
    if i >= len(frame):
        return None
    pl = frame[i]
    hops, size = pl & 0x3F, (pl >> 6) + 1
    if size == 4:
        return None
    i += 1
    path = frame[i : i + hops * size]
    i += hops * size
    if i > len(frame):
        return None
    p = MeshCorePacket(ROUTES[route], TYPES.get(ptype, f"type {ptype}"), ver, path, frame[i:])
    if ptype == 4:
        p.decoded = _advert(p.payload)
    elif ptype == 5 and len(p.payload) > 3:
        ch = p.payload[0]
        for name, key in sorted(keys, key=lambda nk: meshcore_channel_hash(nk[1]) != ch):
            plain = meshcore_decrypt(key, p.payload[1:])
            if plain is not None and len(plain) > 5 and plain[4] >> 2 == 0:
                text = plain[5:].split(b"\0")[0].decode("utf-8", "replace")
                p.decoded = {"channel": name, "time": struct.unpack("<I", plain[:4])[0], "text": text}
                break
    elif ptype in (0, 1, 2, 8) and len(p.payload) >= 2:
        p.decoded = {"dest_hash": p.payload[0], "src_hash": p.payload[1]}
    return p


def _advert(pl: bytes) -> dict:
    if len(pl) < 101:
        return {}
    d = {"key": pl[:32].hex(), "time": struct.unpack("<I", pl[32:36])[0]}
    app = pl[100:]
    if not app:
        return d
    flags = app[0]
    d["node_kind"] = ADVERT_KINDS.get(flags & 0x0F, f"kind {flags & 0x0F}")
    j = 1
    if flags & 0x10 and len(app) >= j + 8:
        lat, lon = struct.unpack("<ii", app[j : j + 8])
        d["lat"], d["lon"] = lat / 1e6, lon / 1e6
        j += 8
    if flags & 0x20:
        j += 2
    if flags & 0x40:
        j += 2
    if flags & 0x80:
        d["name"] = app[j:].split(b"\0")[0].decode("utf-8", "replace")
    return d


def describe_meshcore(p: MeshCorePacket) -> str:
    hops = f"{len(p.path)} path bytes" if p.path else "direct from sender"
    d = p.decoded
    if p.kind == "advert" and d:
        where = f" at {d['lat']:.5f}, {d['lon']:.5f}" if "lat" in d else ""
        return f"advert: {d.get('name', '(no name)')} ({d.get('node_kind', '')}){where}, key {d['key'][:8]}…, {hops}"
    if p.kind == "group text":
        if d:
            return f"[{d['channel']}] {d['text']}, {hops}"
        return f"group text on another channel (hash 0x{p.payload[0]:02x}), {hops}"
    if "src_hash" in d:
        return f"{p.kind} {d['src_hash']:02x} → {d['dest_hash']:02x} (end-to-end encrypted), {hops}"
    return f"{p.kind} ({p.route}), {len(p.payload)} bytes, {hops}"


def build_advert(pubkey: bytes, timestamp: int, name: str, kind=1, lat=None, lon=None) -> bytes:
    """An advert payload with a dummy signature (for tests; receivers here don't verify it)."""
    flags = kind | 0x80 | (0x10 if lat is not None else 0)
    app = bytes([flags])
    if lat is not None:
        app += struct.pack("<ii", int(round(lat * 1e6)), int(round(lon * 1e6)))
    return pubkey + struct.pack("<I", timestamp) + bytes(64) + app + name.encode()


def build_meshcore(ptype: int, payload: bytes, route=1, path=b"") -> bytes:
    return bytes([ptype << 2 | route, len(path)]) + path + payload


def build_group_text(key: bytes, timestamp: int, sender: str, message: str) -> bytes:
    plain = struct.pack("<I", timestamp) + b"\0" + f"{sender}: {message}".encode()
    return bytes([meshcore_channel_hash(key)]) + meshcore_encrypt(key, plain)
