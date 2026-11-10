"""Test signals for every mode."""

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt

from sdr_toolkit.engines.rtty import encode as rtty_codes
from sdr_toolkit.engines.tones import DTMF_HIGH, DTMF_KEYS, DTMF_LOW, DCS_BAUD, dcs_bits


def noise(n, level, seed=0):
    return (np.random.default_rng(seed).standard_normal(n) * level).astype(np.float32)


def read_wav(path):
    with wave.open(str(path)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        if w.getnchannels() == 2:
            a = a[::2]
        return a, w.getframerate()


def fsk(bits, rate, baud, f1, f0, amp=0.5):
    """Continuous-phase FSK: bit 1 at f1, 0 at f0; *bits* may hold fractional-length runs."""
    spb = rate / baud
    n = int(len(bits) * spb)
    idx = np.minimum((np.arange(n) / spb).astype(int), len(bits) - 1)
    f = np.where(np.array(bits)[idx] == 1, f1, f0)
    return (amp * np.sin(2 * np.pi * np.cumsum(f) / rate)).astype(np.float32)


def rtty(text, rate=8000, mark=2125, shift=170, baud=45.45, stop_bits=1.5):
    """Async RTTY: start bit (space), 5 bits LSB first, stop bit(s) (mark); space above mark."""
    halfbits = [1] * 40
    for code in rtty_codes(text):
        halfbits += [0, 0] + sum(([b, b] for b in ((code >> k) & 1 for k in range(5))), []) \
            + [1] * int(stop_bits * 2)
    halfbits += [1] * 40
    return fsk(halfbits, rate, baud * 2, mark, mark + shift)


def dtmf(digits, rate=8000, on=0.08, off=0.08):
    out = []
    for d in digits:
        r = next(i for i, row in enumerate(DTMF_KEYS) if d in row)
        c = DTMF_KEYS[r].index(d)
        t = np.arange(int(on * rate)) / rate
        out += [0.25 * np.sin(2 * np.pi * DTMF_LOW[r] * t) + 0.25 * np.sin(2 * np.pi * DTMF_HIGH[c] * t),
                np.zeros(int(off * rate))]
    return np.concatenate(out).astype(np.float32)


def ctcss_voice(tone, seconds, rate=8000):
    t = np.arange(int(seconds * rate)) / rate
    f0 = 140 + 50 * np.sin(2 * np.pi * 0.5 * t)
    ph = np.cumsum(2 * np.pi * f0 / rate)
    voice = sum(np.sin(k * ph) / k for k in range(3, 25)) * 0.15 * (np.sin(2 * np.pi * 2 * t) > 0)
    return (voice + 0.05 * np.sin(2 * np.pi * tone * t)).astype(np.float32)


def dcs(code, seconds, rate=8000, invert=False):
    words = int(seconds * DCS_BAUD / 23) + 1
    bits = dcs_bits(code, invert) * words
    spb = rate / DCS_BAUD
    idx = (np.arange(int(len(bits) * spb)) / spb).astype(int)
    base = np.where(np.array(bits)[idx] == 1, 1.0, -1.0) * 0.05
    return sosfilt(butter(2, 200, fs=rate, output="sos"), base).astype(np.float32)


# ---- POCSAG (from the standard: BCH(31,21) + even parity, batches of sync + 16 codewords)

POCSAG_SYNC = 0x7CD215D8
POCSAG_IDLE = 0x7A89C197


def _bch(data21):
    gen = 0x769
    cw = data21 << 10
    for i in range(30, 9, -1):
        if cw & (1 << i):
            cw ^= gen << (i - 10)
    cw = (data21 << 10) | (cw & 0x3FF)
    return (cw << 1) | (bin(cw).count("1") & 1)


def pocsag_codewords(address, text, function=3):
    bits = []
    for ch in text:
        bits += [(ord(ch) >> k) & 1 for k in range(7)]
    bits += [0] * (-len(bits) % 20)
    msg = [_bch((1 << 20) | int("".join(map(str, bits[i : i + 20])), 2)) for i in range(0, len(bits), 20)]
    frame = address & 7
    cws = [POCSAG_IDLE] * (2 * frame)
    cws.append(_bch(((address >> 3) << 2) | function))
    cws += msg
    if len(cws) % 16:
        cws += [POCSAG_IDLE] * (16 - len(cws) % 16)
    out = []
    for b in range(0, len(cws), 16):
        out += [POCSAG_SYNC] + cws[b : b + 16]
    return out


def pocsag(address, text, baud=1200, rate=22050, invert=False):
    bits = [1, 0] * 288
    for cw in pocsag_codewords(address, text):
        bits += [(cw >> (31 - k)) & 1 for k in range(32)]
    bits += [1, 0] * 32
    spb = rate / baud
    idx = (np.arange(int(len(bits) * spb)) / spb).astype(int)
    # Bit 1 is the lower frequency: negative in FM discriminator audio.
    level = np.where(np.array(bits)[idx] == 1, -0.4, 0.4)
    if invert:
        level = -level
    return sosfilt(butter(2, baud * 1.5, fs=rate, output="sos"), level).astype(np.float32)


# ---- outside generators

def aprs_packets(rate=48000):
    """direwolf's gen_packets built-in test message, 4 times."""
    d = Path(tempfile.mkdtemp())
    subprocess.run(["gen_packets", "-r", str(rate), "-o", str(d / "p.wav")], capture_output=True, check=True)
    a, r = read_wav(d / "p.wav")
    shutil.rmtree(d)
    return a


def ft8(message, freq=1200, snr=-10):
    """One 15 s FT8 slot from WSJT-X's ft8sim, at 12 kHz."""
    d = Path(tempfile.mkdtemp())
    subprocess.run(["ft8sim", message, str(freq), "0.0", "0.1", "1.0", "1", str(snr)], cwd=d,
                   capture_output=True, check=True)
    a, r = read_wav(next(d.glob("*.wav")))
    shutil.rmtree(d)
    return a


def ft4(message, freq=1200, rate=12000):
    """One 7.5 s FT4 slot, from ft4code's tones (plain FSK, no Gaussian shaping)."""
    out = subprocess.run(["ft4code", message], capture_output=True, text=True, check=True).stdout
    tones = [int(c) for c in out.splitlines()[-1] if c.isdigit()]
    nsps = 576
    f = np.repeat(freq + np.array(tones) * rate / nsps, nsps)
    sig = np.zeros(int(7.5 * rate), np.float32)
    s = 0.3 * np.sin(2 * np.pi * np.cumsum(f) / rate)
    sig[int(0.5 * rate) : int(0.5 * rate) + len(s)] = s
    return sig


def resample(a, rate_from, rate_to):
    g = np.gcd(rate_from, rate_to)
    return resample_poly(a, rate_to // g, rate_from // g).astype(np.float32)


DMR_SYNC_BS_VOICE = 0x755FD7DF75F7
DMR_SYMBOL = {0b01: 3, 0b00: 1, 0b10: -1, 0b11: -3}


def dmr_bursts(rate=48000, seconds=3.0, seed=1):
    """DMR-framed 4FSK as FM discriminator audio (+-1 is +-1944 Hz): 30 ms bursts of CACH, payload,
    the base station voice sync and payload, at 4800 symbols/s. Only the sync is real, so a decoder
    sees DMR frames (slot, a colour code) but no call. Half a second of silence either side."""
    rng = np.random.default_rng(seed)
    sync = [(DMR_SYNC_BS_VOICE >> (47 - i)) & 1 for i in range(48)]
    syms = []
    while len(syms) < seconds * 4800:
        bits = list(rng.integers(0, 2, 132)) + sync + list(rng.integers(0, 2, 108))
        syms += [DMR_SYMBOL[(bits[i] << 1) | bits[i + 1]] for i in range(0, len(bits), 2)]
    a = np.repeat(np.array(syms, np.float32) / 3, rate // 4800)
    pad = np.zeros(rate // 2, np.float32)
    return np.concatenate([pad, a, pad])
