"""Make test audio that sounds like a Talkpod Morse transmission through a receiver.

Follows docs/morse.md in talkpod-super-firmware: 500 ms of carrier, the
message as a hard-keyed 699.99 Hz sine, 3 units + 150 ms, the Roger beep
(1000 Hz then 850 Hz, 80 ms each), a 55 Hz tail tone and the noise burst at
key-down. Optional CTCSS, receiver noise and a receiver audio filter.

    python -m morse_decoder.synth "PARIS" --wpm 30 -o paris.wav
"""

import argparse
import wave

import numpy as np
from scipy.signal import butter, sosfilt

from .decoder import CODE

RATE = 8000


def keying(text: str, wpm: int) -> list[tuple[bool, int]]:
    """The radio's on/off runs in ms for *text*, from the first tone."""
    unit = 1200 // wpm
    runs: list[tuple[bool, int]] = []
    pending_space = False
    for ch in text.upper():
        if ch == " ":
            pending_space = bool(runs)
            continue
        code = CODE.get(ch)
        if not code:
            continue
        if runs:
            runs.append((False, 7 * unit if pending_space else 3 * unit))
        pending_space = False
        for i, el in enumerate(code):
            if i:
                runs.append((False, unit))
            runs.append((True, unit if el == "." else 3 * unit))
    return runs


def transmission(
    text: str,
    wpm: int = 30,
    snr_db: float | None = 25.0,
    ctcss: float | None = 88.5,
    roger: bool = True,
    tail: bool = True,
    receiver: bool = True,
    rate: int = RATE,
    lead_noise_s: float = 0.5,
    seed: int = 1,
    tone_hz: float = 699.99,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    unit = 1200 // wpm
    runs = keying(text, wpm)
    ms = lambda n: int(round(n * rate / 1000))  # noqa: E731

    # Key-up: 500 ms of carrier, then the message, then 3 units + 150 ms.
    gate = [np.zeros(ms(500))]
    for tone, dur in runs:
        gate.append(np.full(ms(dur), 1.0 if tone else 0.0))
    gate.append(np.zeros(ms(3 * unit + 150)))
    gate = np.concatenate(gate)
    t = np.arange(len(gate)) / rate
    sig = 0.5 * gate * np.sin(2 * np.pi * tone_hz * t)
    parts = [sig]
    if roger:
        tb = np.arange(ms(80)) / rate
        parts += [0.4 * np.sin(2 * np.pi * 1000 * tb), 0.4 * np.sin(2 * np.pi * 850 * tb)]
    if tail:
        tt = np.arange(ms(300)) / rate
        parts.append(0.1 * np.sin(2 * np.pi * 55 * tt))
    carrier = np.concatenate(parts)
    if ctcss:
        tc = np.arange(len(carrier)) / rate
        carrier = carrier + 0.08 * np.sin(2 * np.pi * ctcss * tc)
    if snr_db is not None:
        # Receiver noise while the carrier is up, relative to the tone.
        noise = rng.standard_normal(len(carrier)) * 0.5 / np.sqrt(2) * 10 ** (-snr_db / 20)
        carrier = carrier + noise

    # Squelch-open noise before key-up and the key-down burst after it.
    before = 0.3 * rng.standard_normal(ms(lead_noise_s * 1000)) if lead_noise_s else np.zeros(0)
    burst = 0.3 * rng.standard_normal(ms(120))
    out = np.concatenate([before, carrier, burst, np.zeros(ms(800))])
    if receiver:
        sos = butter(4, [250, 3000], btype="band", fs=rate, output="sos")
        out = sosfilt(sos, out)
        # De-emphasis: one pole at 300 Hz (75 us-ish slope across the band).
        a = np.exp(-2 * np.pi * 300 / rate)
        y = np.empty_like(out)
        acc = 0.0
        for i, v in enumerate(out):
            acc = a * acc + (1 - a) * v
            y[i] = acc
        out = y * 4.0
    return np.clip(out, -1, 1).astype(np.float32)


def write_wav(path: str, audio: np.ndarray, rate: int = RATE) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("text")
    p.add_argument("--wpm", type=int, default=30)
    p.add_argument("--snr", type=float, default=25.0, help="dB, tone to noise")
    p.add_argument("--rate", type=int, default=RATE)
    p.add_argument("-o", "--output", required=True)
    a = p.parse_args()
    write_wav(a.output, transmission(a.text, a.wpm, a.snr, rate=a.rate), a.rate)


if __name__ == "__main__":
    main()
