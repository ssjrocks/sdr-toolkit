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


def ais_iq(repeats=4, rate=240000, baud=9600, deviation=2400.0, offset_hz=0.0, gap_s=0.2, payloads=None):
    sps = rate // baud
    t = np.arange(-2 * sps, 2 * sps + 1) / sps
    bt = 0.4
    g = np.exp(-(2 * np.pi**2 * bt**2 / np.log(2)) * t**2)
    g /= g.sum()
    chunks = []
    payloads = payloads or [ais_payload(mmsi=503123456 + r) for r in range(repeats)]
    for p in payloads:
        bits = ais_frame_bits(p)
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


def _text6(s, n_chars):
    from sdr_toolkit.engines.ais import SIXBIT
    s = s.upper().ljust(n_chars, "@")[:n_chars]
    return sum((_bits(SIXBIT.index(c), 6) for c in s), [])


def ais_static_payload(mmsi=503123456, callsign="VJN1234", name="SPIRIT OF TEST", ship_type=70,
                       destination="SYDNEY"):
    """Type 5, static and voyage data (424 bits)."""
    bits = (_bits(5, 6) + _bits(0, 2) + _bits(mmsi, 30) + _bits(0, 2) + _bits(9123456, 30)
            + _text6(callsign, 7) + _text6(name, 20) + _bits(ship_type, 8)
            + _bits(100, 9) + _bits(50, 9) + _bits(10, 6) + _bits(10, 6) + _bits(1, 4)
            + _bits(11, 4) + _bits(9, 5) + _bits(14, 5) + _bits(6, 6) + _bits(85, 8)
            + _text6(destination, 20) + [0] + [0])
    assert len(bits) == 424, len(bits)
    return bits


# ---- ACARS: 2400 bit/s MSK (1200 / 2400 Hz audio tones) on AM, ARINC 618 framing


def _odd_parity(c: int) -> int:
    return c | (0x80 if bin(c).count("1") % 2 == 0 else 0)


def _crc16_kermit(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c


def acars_frame(mode="2", reg=".VH-OQA", label="H1", block_id="1", text="M01AQF0001HELLO FROM THE TEST",
                ack="\x15") -> bytes:
    """One ACARS block as sent: pre-key, '+', '*', SYN SYN SOH, the block (odd parity), ETX, CRC, DEL. A
    downlink's text starts with the message number (4) and flight (6)."""
    body = bytes(_odd_parity(ord(c)) for c in mode + reg + ack + label + block_id) + bytes([_odd_parity(2)]) \
        + bytes(_odd_parity(ord(c)) for c in text) + bytes([0x83])
    c = _crc16_kermit(body)
    return bytes([0xFF] * 16 + [0xAB, 0x2A, 0x16, 0x16, 0x01]) + body + bytes([c & 0xFF, c >> 8, 0x7F])


def acars_audio(frame: bytes, rate: float) -> np.ndarray:
    """The MSK audio: LSB first; a bit equal to the one before is sent as 2400 Hz, a change as 1200 Hz
    (continuous phase), as acarsdec's demodulator (msk.c) expects."""
    bits = [(b >> i) & 1 for b in frame for i in range(8)]
    spb = rate / 2400
    out, ph, prev = [], 0.0, 1
    for n, b in enumerate(bits):
        tone = 2400 if b == prev else 1200
        prev = b
        m = int(round((n + 1) * spb)) - int(round(n * spb))
        out.append(np.sin(ph + 2 * np.pi * tone * np.arange(m) / rate))
        ph = (ph + 2 * np.pi * tone * m / rate) % (2 * np.pi)
    return np.concatenate(out)


def acars_iq(rate=2_400_000, offset_hz=0.0, frames=None, gap_s=0.3, seed=1):
    """AM-modulated ACARS blocks at *offset_hz* from the recording's centre, with a little noise."""
    frames = frames or [acars_frame()]
    pad = np.zeros(int(gap_s * rate))
    parts = [pad]
    for f in frames:
        parts += [acars_audio(f, rate), pad]
    a = np.concatenate(parts)
    x = 0.3 * (1 + 0.6 * a) * np.exp(2j * np.pi * offset_hz * np.arange(len(a)) / rate)
    return (x + _noise(len(x), 0.01, seed)).astype(np.complex64)


# ---- VOR and ILS (ICAO Annex 10): AM tones on a carrier, with a 1020 Hz Morse ident


def _ident_keying(ident, rate, n, wpm=7, start_s=1.0):
    """A 0/1 envelope n samples long: *ident* in Morse, repeated every 8 s from start_s."""
    from sdr_toolkit.engines.morse.synth import keying
    k = np.zeros(n)
    i = int(start_s * rate)
    while i < n:
        j = i
        for tone, ms in keying(ident, wpm):
            m = int(ms * rate / 1000)
            if tone:
                k[j:j + m] = 1.0
            j += m
        i += int(8 * rate)
    return k


def _am_carrier(audio, rate, offset_hz, seed, level=0.3):
    x = level * (1 + audio) * np.exp(2j * np.pi * offset_hz * np.arange(len(audio)) / rate)
    return (x + _noise(len(x), 0.003, seed)).astype(np.complex64)


def vor_iq(radial=123.0, ident="SY", seconds=10.0, rate=480000, offset_hz=0.0, seed=2):
    """A conventional VOR seen from *radial* (degrees from the station): 30 Hz AM 30% (the variable, lagging
    the reference by the radial), a 9960 Hz subcarrier 30% FM +-480 Hz by the 30 Hz reference, ident 10%."""
    n = int(seconds * rate)
    t = np.arange(n) / rate
    w = 2 * np.pi * 30.0
    audio = 0.3 * np.cos(w * t - np.radians(radial))
    audio += 0.3 * np.cos(2 * np.pi * 9960.0 * t + (480.0 / 30.0) * np.sin(w * t))
    audio += 0.1 * _ident_keying(ident, rate, n) * np.sin(2 * np.pi * 1020.0 * t)
    return _am_carrier(audio, rate, offset_hz, seed)


def ils_iq(ddm=0.05, sdm=0.4, ident="ISY", seconds=10.0, rate=480000, offset_hz=0.0, seed=3):
    """An ILS localizer (sdm 0.4) or glide slope (sdm 0.8): 90 Hz and 150 Hz AM whose depths differ by *ddm*
    (90 minus 150)."""
    n = int(seconds * rate)
    t = np.arange(n) / rate
    audio = (sdm + ddm) / 2 * np.sin(2 * np.pi * 90.0 * t) + (sdm - ddm) / 2 * np.sin(2 * np.pi * 150.0 * t)
    if ident:
        audio += 0.1 * _ident_keying(ident, rate, n) * np.sin(2 * np.pi * 1020.0 * t)
    return _am_carrier(audio, rate, offset_hz, seed)


# ---- Marine DSC (ITU-R M.493): 1200 baud FSK, 2100 Hz = B (0), 1300 Hz = Y (1), on FM


def dsc_symbols(fmt, body, eos=127):
    """The characters of a call as sent: phasing, then each character in a DX position and again 4 characters
    later in an RX position, then EOS (3 more times in DX) and the error-check character."""
    from sdr_toolkit.engines.dsc import PHASING_DX, PHASING_RX
    info = [fmt, fmt] + list(body) + [eos]
    ecc = 0
    for c in [fmt] + list(body) + [eos]:
        ecc ^= c
    msg = info + [ecc]
    # Stream positions: DX k at 2k, RX k at 2k+1; DX has 6 phasing then msg; RX has 8 phasing then msg.
    dx = [PHASING_DX] * 6 + msg + [eos, eos]
    rx = list(PHASING_RX) + msg
    out = []
    for k in range(max(len(dx), len(rx))):
        out.append(dx[k] if k < len(dx) else eos)
        if k < len(rx):
            out.append(rx[k])
    return out


def dsc_audio(symbols, rate=48000, dots=20, lead_s=0.3):
    from sdr_toolkit.engines.dsc import encode
    bits = [i % 2 for i in range(dots)] + [b for s in symbols for b in encode(s)]
    spb = rate / 1200
    out, ph = [np.zeros(int(lead_s * rate))], 0.0
    for n, b in enumerate(bits):
        f = 1300.0 if b else 2100.0
        m = int(round((n + 1) * spb)) - int(round(n * spb))
        out.append(np.sin(ph + 2 * np.pi * f * np.arange(m) / rate))
        ph = (ph + 2 * np.pi * f * m / rate) % (2 * np.pi)
    out.append(np.zeros(int(lead_s * rate)))
    return np.concatenate(out)


def fm_iq(audio, audio_rate=48000, rate=240000, deviation=3000.0, seed=4, noise=0.02):
    """Audio FM-modulated onto a carrier at the centre of an IQ recording."""
    from scipy.signal import resample_poly
    up = resample_poly(audio, rate // audio_rate, 1)
    ph = 2 * np.pi * deviation * np.cumsum(up) / rate
    x = 0.5 * np.exp(1j * ph)
    return (x + _noise(len(x), noise, seed)).astype(np.complex64)


# ---- 406 MHz distress beacons (C/S T.001, first generation): 160 ms carrier, then biphase-L +-1.1 rad at 400 bps


def beacon406_iq(frame_bits, rate=960000, offset_hz=200000.0, carrier_hz=2000.0, level=0.3, seed=5, lead_s=0.4,
                 noise=0.01):
    """One burst: *frame_bits* (from the bit sync, 112 or 144) after 160 ms of plain carrier, *carrier_hz* above
    the 406.05 MHz band centre, which sits *offset_hz* from the recording's centre."""
    spb = rate / 400
    ph = []
    for b in frame_bits:
        h = int(spb / 2)
        first = 1.1 if b == "1" else -1.1
        ph += [first] * h + [-first] * (int(spb) - h)
    phase = np.concatenate([np.zeros(int(0.16 * rate)), np.array(ph)])
    n_lead = int(lead_s * rate)
    env = np.concatenate([np.zeros(n_lead), np.ones(len(phase)), np.zeros(n_lead)])
    phase = np.concatenate([np.zeros(n_lead), phase, np.zeros(n_lead)])
    t = np.arange(len(env)) / rate
    x = level * env * np.exp(1j * (phase + 2 * np.pi * (offset_hz + carrier_hz) * t + 0.7))
    return (x + _noise(len(x), noise, seed)).astype(np.complex64)
