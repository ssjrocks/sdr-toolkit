"""GPS L1 recordings for the tests, made with gps-sdr-sim (github.com/osqzss/gps-sdr-sim).

gps-sdr-sim writes the satellites' signals for a position and time from a real
broadcast ephemeris, as signed 8-bit IQ without noise. ``recording()`` turns that
into what a dongle with a crystal *ppm* fast would record: its sample clock fast
(the simulation is made at that rate), its tuning high (the signal shifted
down), noise, and unsigned bytes as rtl_sdr writes them.
"""

import shutil
import subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SIM = ROOT / "build" / "gps-sdr-sim"


def simulator():
    exe = SIM / "gps-sdr-sim"
    if exe.is_file():
        return exe, SIM / "brdc0010.22n"
    return (shutil.which("gps-sdr-sim"), None)


def recording(path, lat, lon, height=40.0, seconds=75, ppm=0.0, snr_db=-12.0):
    exe, nav = simulator()
    rate = round(2048000 * (1 + ppm * 1e-6) / 10) * 10        # gps-sdr-sim rounds to 10 Hz
    ppm = (rate / 2048000 - 1) * 1e6
    raw = Path(path).with_suffix(".s8")
    subprocess.run([str(exe), "-e", str(nav), "-l", f"{lat},{lon},{height}", "-d", str(seconds),
                    "-s", str(rate), "-b", "8", "-o", str(raw)], check=True, capture_output=True)
    src = np.memmap(raw, np.int8, "r")
    n = len(src) // 2
    probe = src[: 400000].astype(np.float32)
    sig_pow = float(np.mean(probe**2)) * 2
    noise_sd = np.sqrt(sig_pow / 10 ** (snr_db / 10) / 2)
    scale = 30 / np.sqrt(sig_pow + 2 * noise_sd**2)
    shift = -1575.42e6 * ppm * 1e-6
    rng = np.random.default_rng(1)
    with open(path, "wb") as out:
        chunk = 1 << 20
        for i in range(0, n, chunk):
            m = min(chunk, n - i)
            x = src[2 * i: 2 * (i + m)].astype(np.float32)
            z = (x[0::2] + 1j * x[1::2]) * np.exp(2j * np.pi * shift * (np.arange(m) + i) / rate)
            z = (z + noise_sd * (rng.standard_normal(m) + 1j * rng.standard_normal(m))) * scale
            iq = np.empty(2 * m, np.float32)
            iq[0::2], iq[1::2] = z.real, z.imag
            out.write(np.clip(np.round(iq + 127.5), 0, 255).astype(np.uint8).tobytes())
    del src
    raw.unlink()
    return path
