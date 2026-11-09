"""IQ test signals for the RTL-SDR modes, written as .cu8 recordings."""

import math

import numpy as np
from scipy.signal import butter, sosfilt

from sdr_toolkit.modes.radio import write_cu8


def _noise(n, level, seed):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * level


# ---- ISM: a Nexus-TH temperature/humidity sensor (rtl_433 protocol 19)

def nexus_bits(sensor_id, channel, temp_c, humidity, battery=1):
    t = int(round(temp_c * 10)) & 0xFFF
    v = (sensor_id << 28) | (battery << 27) | ((channel - 1) << 24) | (t << 12) | (0xF << 8) | humidity
    return [(v >> (35 - i)) & 1 for i in range(36)]


def nexus_iq(sensor_id=0xA5, channel=2, temp_c=21.7, humidity=58, rate=250000, offset_hz=40e3):
    us = lambda s: int(round(s * rate))  # noqa: E731
    env = [np.zeros(us(0.05))]
    for _ in range(12):
        for b in nexus_bits(sensor_id, channel, temp_c, humidity):
            env += [np.ones(us(500e-6)), np.zeros(us(2000e-6 if b else 1000e-6))]
        env += [np.ones(us(500e-6)), np.zeros(us(4000e-6))]
    env.append(np.zeros(us(0.05)))
    e = np.concatenate(env)
    n = np.arange(len(e))
    return 0.6 * e * np.exp(2j * np.pi * offset_hz * n / rate) + _noise(len(e), 0.02, 0)


# ---- ADS-B: Mode S DF17 extended squitter

def crc24(bits88):
    poly = 0x1FFF409
    v = int("".join(map(str, bits88)), 2) << 24
    for i in range(111, 23, -1):
        if v & (1 << i):
            v ^= poly << (i - 24)
    return v & 0xFFFFFF


def df17(icao, me_bits):
    head = [(17 >> (4 - i)) & 1 for i in range(5)] + [1, 0, 1] + [(icao >> (23 - i)) & 1 for i in range(24)]
    bits = head + me_bits
    pi = crc24(bits)
    return bits + [(pi >> (23 - i)) & 1 for i in range(24)]


def _bits(v, n):
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


def ident_me(callsign):
    cs = callsign.upper().ljust(8)[:8]
    bits = _bits(4, 5) + _bits(0, 3)
    for c in cs:
        bits += _bits(CHARS.index(c), 6)
    return bits


def _nl(lat):
    if lat == 0:
        return 59
    if abs(lat) == 87:
        return 2
    if abs(lat) > 87:
        return 1
    a = 1 - (1 - math.cos(math.pi / 30)) / math.cos(math.pi / 180 * abs(lat)) ** 2
    return math.floor(2 * math.pi / math.acos(a))


def position_me(lat, lon, alt_ft, odd):
    i = 1 if odd else 0
    dlat = 360 / (60 - i)
    yz = math.floor(2**17 * (lat % dlat) / dlat + 0.5)
    rlat = dlat * (yz / 2**17 + math.floor(lat / dlat))
    dlon = 360 / max(_nl(rlat) - i, 1)
    xz = math.floor(2**17 * (lon % dlon) / dlon + 0.5)
    n = int(round((alt_ft + 1000) / 25))
    alt = ((n >> 4) << 5) | (1 << 4) | (n & 0xF)
    return (_bits(11, 5) + _bits(0, 2) + [0] + _bits(alt, 12) + [0, i]
            + _bits(yz & 0x1FFFF, 17) + _bits(xz & 0x1FFFF, 17))


def adsb_iq(icao=0x7C1234, callsign="QFA123", lat=-33.95, lon=151.18, alt_ft=12000,
            seconds=3.0, rate=2400000, level=0.5):
    """Identification and even/odd position squitters, each every 0.1 s."""
    over = 10
    hi = rate * over
    frames = [df17(icao, ident_me(callsign)), df17(icao, position_me(lat, lon, alt_ft, False)),
              df17(icao, position_me(lat, lon, alt_ft, True))]
    n = int(seconds * rate)
    env = np.zeros(n * over, np.float32)
    us = hi // 1_000_000

    def pulse(at_us, start, width=0.5):
        a = start + int(at_us * us)
        env[a : a + int(width * us)] = 1.0

    t, k = int(0.05 * hi), 0
    while t + 130 * us < len(env):
        bits = frames[k % len(frames)]
        for p in (0, 1.0, 3.5, 4.5):
            pulse(p, t)
        for j, b in enumerate(bits):
            pulse(8 + j + (0 if b else 0.5), t)
        t += int(0.1 / len(frames) * hi)
        k += 1
    e = env.reshape(-1, over).mean(axis=1)
    iq = level * e * np.exp(1j * (2 * np.pi * 50e3 * np.arange(n) / rate + 0.7))
    return iq + _noise(n, 0.03, 1)


# ---- AIS: a type 1 position report, HDLC framed, NRZI, GMSK at 9600 baud

def _crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def ais_payload(mmsi=503123456, lat=-33.85, lon=151.21, sog=12.3, cog=87.0, heading=88):
    bits = (_bits(1, 6) + _bits(0, 2) + _bits(mmsi, 30) + _bits(0, 4) + _bits(128, 8)
            + _bits(int(round(sog * 10)), 10) + [1]
            + _bits(int(round(lon * 600000)) & ((1 << 28) - 1), 28)
            + _bits(int(round(lat * 600000)) & ((1 << 27) - 1), 27)
            + _bits(int(round(cog * 10)), 12) + _bits(heading, 9) + _bits(30, 6)
            + _bits(0, 2) + _bits(0, 3) + [0] + _bits(0, 19))
    return bits


def ais_frame_bits(payload_bits):
    data = bytes(int("".join(map(str, payload_bits[i : i + 8])), 2) for i in range(0, len(payload_bits), 8))
    crc = _crc16_x25(data)
    body = []
    for byte in data + bytes([crc & 0xFF, crc >> 8]):
        body += [(byte >> i) & 1 for i in range(8)]          # each byte least significant bit first
    stuffed, ones = [], 0
    for b in body:
        stuffed.append(b)
        ones = ones + 1 if b else 0
        if ones == 5:
            stuffed.append(0)
            ones = 0
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    raw = [0, 1] * 12 + flag + stuffed + flag + [0] * 8
    out, level = [], 1
    for b in raw:                                           # NRZI: a 0 toggles
        if b == 0:
            level ^= 1
        out.append(level)
    return out


def ais_iq(repeats=4, rate=240000, baud=9600, deviation=2400.0, offset_hz=0.0, gap_s=0.2):
    sps = rate // baud
    t = np.arange(-2 * sps, 2 * sps + 1) / sps
    bt = 0.4
    g = np.exp(-(2 * np.pi**2 * bt**2 / np.log(2)) * t**2)
    g /= g.sum()
    chunks = []
    for r in range(repeats):
        bits = ais_frame_bits(ais_payload(mmsi=503123456 + r))
        nrz = np.repeat(np.array(bits) * 2.0 - 1, sps)
        f = np.convolve(nrz, g, mode="same") * deviation + offset_hz
        ph = 2 * np.pi * np.cumsum(f) / rate
        chunks += [np.exp(1j * ph), np.zeros(int(gap_s * rate))]
    x = np.concatenate([np.zeros(int(0.1 * rate))] + chunks)
    return 0.6 * x + _noise(len(x), 0.02, 2)


def save(path, iq):
    write_cu8(path, iq)
    return path


def lowpass(iq, rate, cutoff):
    return sosfilt(butter(4, cutoff, fs=rate, output="sos"), iq)
