"""Bluetooth LE advertisements: what each device says about itself, and drone Remote ID.

classify() names a device from its advertisement (maker, kind, trackers such as AirTags, Samsung SmartTags and
Tiles, beacons); remote_id() reads ASTM F3411 / ASD-STAN Open Drone ID messages (the drone's ID, position, speed
and height, and its pilot's position) from service data 0xFFFA. Both are pure functions of the advertisement:
the scanner (ble_scan.py) feeds them.
"""

import struct

COMPANIES = {
    0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google", 0x0087: "Garmin",
    0x0157: "Huami (Amazfit)", 0x038F: "Xiaomi", 0x0059: "Nordic Semiconductor", 0x000F: "Broadcom",
    0x0171: "Amazon", 0x02E5: "Espressif", 0x0499: "Ruuvi", 0x0131: "Cypress", 0x0001: "Nokia", 0x0002: "Intel",
    0x000A: "Qualcomm", 0x001D: "Qualcomm", 0x0046: "MediaTek", 0x00D2: "Dialog", 0x010F: "Huawei", 0x027D: "Huawei",
    0x0310: "SGL", 0x01DA: "Logitech", 0x0822: "Shelly", 0x0969: "Govee", 0x8801: "Govee", 0x02D5: "Omron",
    0x0101: "Fugoo", 0x0180: "Gibson", 0x00CD: "Microchip", 0x0217: "Tile", 0x067C: "Tile", 0x05A7: "Sonos",
    0x0057: "Harman", 0x012D: "Sony", 0x0301: "Bose", 0x009E: "Bose", 0x00C4: "LG", 0x0072: "Polar",
    0x02FF: "Silicon Labs", 0x0047: "Bluegiga", 0x00A0: "Kensington", 0x05F1: "Linux Foundation",
    0x07D0: "Tuya",
}
# 16-bit service UUIDs that say what a device is.
SERVICES = {
    0xFD5A: ("Samsung SmartTag", "tracker"), 0xFD59: ("Samsung SmartTag", "tracker"),
    0xFEED: ("Tile tracker", "tracker"), 0xFEEC: ("Tile tracker", "tracker"), 0xFE33: ("Chipolo tracker", "tracker"),
    0xFE2C: ("Google Fast Pair device", ""), 0xFEAA: ("Eddystone beacon", "beacon"),
    0xFD6F: ("Exposure notification (phone)", "phone"), 0xFE9F: ("Google device", ""),
    0xFE03: ("Amazon device", ""), 0xFCF1: ("Google device", ""), 0xFE8F: ("CSR / Qualcomm", ""),
    0x180D: ("Heart-rate sensor", "sensor"), 0x181A: ("Environmental sensor", "sensor"),
    0x1809: ("Thermometer", "sensor"), 0x1816: ("Bike speed / cadence", "sensor"), 0x1818: ("Bike power", "sensor"),
    0x1812: ("Keyboard / mouse (HID)", ""), 0x180F: ("Battery service", ""), 0xFFFA: ("Drone Remote ID", "drone"),
    0xFCD2: ("BTHome sensor", "sensor"), 0x181C: ("User data", ""), 0xFE95: ("Xiaomi device", ""),
}
APPLE_TYPES = {0x02: ("iBeacon", "beacon"), 0x05: ("Apple AirDrop", "phone"), 0x07: ("Apple AirPods / Beats", "audio"),
               0x09: ("Apple AirPlay", ""), 0x0C: ("Apple Handoff", "phone"), 0x0F: ("Apple Nearby Action", "phone"),
               0x10: ("Apple device (Nearby Info)", "phone"), 0x12: ("Apple Find My", "tracker"),
               0x16: ("Apple Nearby Info", "phone")}


def uuid16(u: str) -> int | None:
    """The 16-bit form of a Bluetooth base UUID ("0000xxxx-0000-1000-8000-00805f9b34fb"), or None."""
    u = str(u).lower()
    if len(u) == 36 and u.endswith("-0000-1000-8000-00805f9b34fb") and u.startswith("0000"):
        return int(u[4:8], 16)
    if len(u) == 4:
        return int(u, 16)
    return None


def classify(name: str, manufacturer: dict, services: list, service_data: dict) -> dict:
    """{maker, kind, what, tracker, extra}: from the advertisement's name, manufacturer data ({company: bytes}),
    service UUIDs and service data ({uuid: bytes})."""
    out = {"maker": "", "kind": "", "what": "", "tracker": False, "extra": {}}
    for cid, data in manufacturer.items():
        out["maker"] = COMPANIES.get(cid, f"company 0x{cid:04X}")
        if cid == 0x004C and data:
            t = data[0]
            what, kind = APPLE_TYPES.get(t, ("Apple device", ""))
            out["what"], out["kind"] = what, kind
            if t == 0x02 and len(data) >= 23:                    # iBeacon: UUID, major, minor, power
                u = data[2:18].hex()
                out["extra"] = {"uuid": f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}",
                                "major": int.from_bytes(data[18:20], "big"), "minor": int.from_bytes(data[20:22], "big"),
                                "power": struct.unpack("b", data[22:23])[0]}
            if t == 0x12 and len(data) >= 3:
                # Find My (AirTags, AirPods, iPhones...): the long form (length 0x19, its full public key) once it's
                # been away from its owner for a while, the short form (0x02) near its owner.
                out["tracker"] = True
                out["extra"]["status"] = data[2]
                away = data[1] >= 0x19
                out["extra"]["away_from_owner"] = away
                out["what"] = ("Apple Find My, away from its owner (an AirTag or Apple device)" if away
                               else "Apple Find My, near its owner (an AirTag or Apple device)")
        if cid == 0x0006:
            out["what"] = "Microsoft device (Windows / Swift Pair)"
    for u in list(services) + list(service_data):
        s = uuid16(u)
        if s in SERVICES:
            what, kind = SERVICES[s]
            if not out["what"] or kind in ("tracker", "drone"):
                out["what"], out["kind"] = what, kind or out["kind"]
            if kind == "tracker":
                out["tracker"] = True
    if not out["what"] and name:
        low = name.lower()
        for word, what, kind in (("tv", "TV", ""), ("watch", "Watch", "wearable"), ("band", "Fitness band", "wearable"),
                                 ("buds", "Earbuds", "audio"), ("airpods", "AirPods", "audio"), ("tile", "Tile", "tracker"),
                                 ("printer", "Printer", ""), ("car", "Car", ""), ("speaker", "Speaker", "audio")):
            if word in low:
                out["what"], out["kind"] = what, kind
                out["tracker"] = out["tracker"] or kind == "tracker"
                break
    return out


# ---- Open Drone ID (ASTM F3411) --------------------------------------------------------------------

UA_TYPES = ("", "aeroplane", "helicopter / multirotor", "gyroplane", "VTOL", "ornithopter", "glider", "kite",
            "free balloon", "captive balloon", "airship", "parachute", "rocket", "tethered", "ground obstacle", "other")
ID_TYPES = ("", "serial number", "CAA registration", "UTM-assigned", "session ID")


def _alt(raw: int) -> float | None:
    return None if raw == 0 else round(raw * 0.5 - 1000, 1)


def drone_message(m: bytes, out: dict):
    """One 25-byte Open Drone ID message into *out*."""
    if len(m) < 25:
        return
    t = m[0] >> 4
    if t == 0:                                                   # Basic ID
        out["id_type"] = ID_TYPES[m[1] >> 4] if (m[1] >> 4) < len(ID_TYPES) else str(m[1] >> 4)
        out["ua_type"] = UA_TYPES[m[1] & 0xF]
        out["id"] = m[2:22].rstrip(b"\0").decode("ascii", "replace")
    elif t == 1:                                                 # Location / vector
        flags = m[1]
        dirn = m[2] + (180 if flags & 0x02 else 0)
        mult = flags & 0x01
        spd = m[3] * 0.25 if not mult else m[3] * 0.75 + 63.75
        lat, lon = struct.unpack_from("<ii", m, 5)
        out.update(status=("undeclared", "on ground", "airborne", "emergency", "remote ID failure")[min(m[1] >> 4, 4)],
                   track=dirn, speed_ms=round(spd, 2), climb_ms=round(struct.unpack_from("b", m, 4)[0] * 0.5, 1))
        if lat or lon:
            out["lat"], out["lon"] = lat * 1e-7, lon * 1e-7
        p, g, h = struct.unpack_from("<HHH", m, 13)
        out["alt_baro"], out["alt_geo"], out["height"] = _alt(p), _alt(g), _alt(h)
    elif t == 3:                                                 # Self ID: free text
        out["description"] = m[2:25].rstrip(b"\0").decode("ascii", "replace")
    elif t == 4:                                                 # System: the operator's position
        lat, lon = struct.unpack_from("<ii", m, 2)
        if lat or lon:
            out["op_lat"], out["op_lon"] = lat * 1e-7, lon * 1e-7
        out["op_alt"] = _alt(struct.unpack_from("<H", m, 18)[0])
    elif t == 5:                                                 # Operator ID
        out["operator"] = m[2:22].rstrip(b"\0").decode("ascii", "replace")
    elif t == 0xF:                                               # message pack
        size, n = m[1], m[2]
        for i in range(n):
            drone_message(m[3 + i * size:3 + (i + 1) * size], out)


def remote_id(service_data: dict) -> dict | None:
    """The drone's Remote ID from an advertisement's service data, or None. Bluetooth 4 legacy advertising
    carries [0x0D application code][counter][one 25-byte message] (a pack on Bluetooth 5)."""
    for u, data in service_data.items():
        if uuid16(u) != 0xFFFA or len(data) < 3:
            continue
        out: dict = {}
        payload = data[2:] if data[0] == 0x0D else data
        drone_message(payload if len(payload) >= 25 else payload.ljust(25, b"\0"), out)
        return out
    return None
