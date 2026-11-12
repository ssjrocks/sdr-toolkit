"""The receiver's signal processing: spectrum, channel filter and demodulators.

Everything works block by block with its state carried over (filters, phase, the
decimation phase), so audio has no clicks at block edges. Sample rates are whole
multiples of 48 kHz, so every rate change on the way to 48 kHz audio is a whole-number
decimation: a filter at the higher rate, then every n-th sample.
"""

import numpy as np
from scipy.signal import butter, lfilter, sosfilt, sosfilt_zi

AUDIO_RATE = 48000
# Sample rates offered: whole multiples of 48 kHz the RTL2832 can deliver.
RATES = (2880000, 2400000, 1920000, 1440000, 1200000, 960000, 288000, 240000)

MODES = {
    # mode: (default bandwidth Hz, min, max, help)
    "WFM": (180000, 50000, 220000, "broadcast FM"),
    "NFM": (12500, 3000, 25000, "narrow FM: two-way radio, amateur repeaters"),
    "AM": (10000, 2000, 20000, "airband, broadcast AM"),
    "USB": (2700, 500, 5000, "upper sideband: amateur HF voice above 10 MHz, digital modes"),
    "LSB": (2700, 500, 5000, "lower sideband: amateur HF voice below 10 MHz"),
    "CW": (500, 50, 2000, "Morse, heard as a 700 Hz tone"),
    "RAW": (48000, 10000, 48000, "no demodulation (spectrum only)"),
}
CW_TONE = 700.0
AGC_RELEASE = 1.0          # s for the AGC's gain to come halfway back up after a signal
AGC_MAX_GAIN = 2000.0

# Tuner gain steps (tenths of a dB), as librtlsdr lists them for each tuner type.
TUNER_GAINS = {
    1: (-10, 15, 40, 65, 90, 115, 140, 165, 190, 215, 240, 290, 340, 420),                  # E4000
    2: (-99, -40, 71, 179, 192),                                                            # FC0012
    3: (-99, -73, -65, -63, -60, -58, -54, 58, 61, 63, 65, 67, 68, 70, 71, 179, 181, 182, 184, 186, 188,
        191, 197),                                                                          # FC0013
    4: (0,),                                                                                # FC2580
    5: (0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372,
        386, 402, 421, 434, 439, 445, 480, 496),                                            # R820T
    6: (0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372,
        386, 402, 421, 434, 439, 445, 480, 496),                                            # R828D
}
R820T_GAINS = TUNER_GAINS[5]


def cu8(raw: bytes) -> np.ndarray:
    a = np.frombuffer(raw, np.uint8)[: len(raw) // 2 * 2].astype(np.float32)
    a = (a - 127.5) / 127.5
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


class Spectrum:
    """Averaged power spectrum in dBFS, centre in the middle."""

    def __init__(self, size=4096, average=0.5):
        self.size = size
        self.average = average            # 0 = none, towards 1 = slower
        self.window = np.blackman(size).astype(np.float32)
        self.norm = 20 * np.log10(np.sum(self.window))
        self.db = None

    def update(self, x: np.ndarray):
        if len(x) < self.size:
            return self.db
        seg = x[-self.size:] * self.window
        p = np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
        db = (10 * np.log10(p + 1e-20) - self.norm).astype(np.float32)     # full-scale tone = 0 dBFS
        if self.db is None or len(self.db) != len(db) or not self.average:
            self.db = db
        else:
            # Average the power, not the dB, so peaks don't get pulled down.
            lin = (1 - self.average) * 10 ** (db / 10) + self.average * 10 ** (self.db / 10)
            self.db = (10 * np.log10(lin + 1e-20)).astype(np.float32)
        return self.db


class Decimator:
    """Low-pass (Butterworth, stateful) then every n-th sample, keeping the phase across blocks."""

    def __init__(self, rate, factor, cutoff, order=6):
        self.factor = int(factor)
        self.sos = butter(order, min(cutoff, rate / 2 * 0.95), fs=rate, output="sos") if factor > 1 or cutoff < rate / 2 * 0.95 else None
        self.zi = None
        self.skip = 0

    def __call__(self, x):
        if self.sos is not None:
            if self.zi is None:
                self.zi = (sosfilt_zi(self.sos) * (x[0] if len(x) else 0)).astype(np.result_type(x, np.float64))
                if np.iscomplexobj(x):
                    self.zi = self.zi.astype(complex)
            x, self.zi = sosfilt(self.sos, x, zi=self.zi)
        if self.factor == 1:
            return x
        out = x[self.skip::self.factor]
        self.skip = (self.skip - len(x)) % self.factor
        return out


class Mixer:
    """Shift a frequency to 0 Hz, continuing the phase between blocks."""

    def __init__(self, rate, shift_hz=0.0):
        self.rate = rate
        self.phase = 0.0
        self.set(shift_hz)

    def set(self, shift_hz):
        self.shift = float(shift_hz)
        self.step = -2 * np.pi * self.shift / self.rate

    def __call__(self, x):
        if not self.shift:
            return x
        n = np.arange(len(x))
        out = x * np.exp(1j * (self.phase + self.step * n)).astype(np.complex64)
        self.phase = (self.phase + self.step * len(x)) % (2 * np.pi)
        return out


def _divisors(n):
    return [d for d in range(1, n + 1) if n % d == 0]


class Demodulator:
    """One channel: shift it to 0 Hz, filter to its bandwidth, demodulate, 48 kHz audio out."""

    def __init__(self, rate, mode="NFM", offset=0.0, bandwidth=None, deemphasis=50e-6, squelch=None):
        if rate % AUDIO_RATE:
            raise ValueError(f"sample rate {rate} isn't a multiple of {AUDIO_RATE}")
        self.rate = int(rate)
        self.mode = mode
        self.bandwidth = float(bandwidth or MODES[mode][0])
        self.deemphasis = deemphasis
        self.squelch = squelch                # dBFS of the channel, None = open
        self.level = -120.0                   # channel power, dBFS
        self.open = True
        self._peak = 0.0                    # the AGC's idea of the signal's peak level
        self.mixer = Mixer(self.rate, offset)
        self._build()

    @property
    def offset(self):
        return self.mixer.shift

    def retune(self, offset):
        self.mixer.set(offset)

    def _build(self):
        total = self.rate // AUDIO_RATE
        bw = self.bandwidth
        if self.mode == "WFM":
            # The channel at >= 200 kHz (FM is ~180 kHz wide), then down to 48 kHz after demodulation.
            d1 = max(d for d in _divisors(total) if self.rate // d >= 200000) if self.rate >= 200000 else 1
            self.if_rate = self.rate // d1
            self.chan = Decimator(self.rate, d1, bw / 2)
            self.audio = Decimator(self.if_rate, self.if_rate // AUDIO_RATE, 15000)
            tau = self.deemphasis
            a = np.exp(-1 / (self.if_rate * tau)) if tau else 0.0
            self.de_b, self.de_a, self.de_z = 1 - a, a, 0.0
        else:
            self.if_rate = AUDIO_RATE
            self.chan = Decimator(self.rate, total, max(bw / 2, 500) if self.mode not in ("USB", "LSB", "CW")
                                  else max(bw, 600) + 300)
            self.audio = None
            # Sideband selection at 48 kHz: move the passband's middle to 0 Hz, filter to half the
            # bandwidth, move it back; the real part is the audio. USB's passband is 100 Hz to
            # 100 Hz + bandwidth above the carrier, LSB's the same below. CW: the signal at 0 Hz,
            # filtered narrow, then moved up to the 700 Hz tone.
            half = bw / 2
            into, back = {"USB": (half + 100, half + 100), "LSB": (-(half + 100), -(half + 100)),
                          "CW": (0.0, -CW_TONE)}.get(self.mode, (0.0, 0.0))
            self.ssb_in = Mixer(AUDIO_RATE, into)
            self.ssb_out = Mixer(AUDIO_RATE, -back)
            self.ssb = Decimator(AUDIO_RATE, 1, half)
        self._last = 1 + 0j
        self._dc = 0.0

    def set(self, mode=None, bandwidth=None, deemphasis=None):
        changed = False
        if mode is not None and mode != self.mode:
            self.mode = mode
            if bandwidth is None:
                self.bandwidth = float(MODES[mode][0])
            changed = True
        if bandwidth is not None and float(bandwidth) != self.bandwidth:
            self.bandwidth = float(bandwidth)
            changed = True
        if deemphasis is not None and deemphasis != self.deemphasis:
            self.deemphasis = deemphasis
            changed = True
        if changed:
            self._build()

    def process(self, x: np.ndarray) -> np.ndarray:
        """Complex samples at the input rate -> float32 audio at 48 kHz, -1..1."""
        y = self.chan(self.mixer(x))
        if len(y):
            p = float(np.mean(np.abs(y) ** 2))
            self.level = 10 * np.log10(p + 1e-20)
        self.open = self.squelch is None or self.level >= self.squelch
        m = self.mode
        if m in ("WFM", "NFM"):
            prev = np.concatenate([[self._last], y[:-1]]) if len(y) else y
            if len(y):
                self._last = y[-1]
            disc = np.angle(y * np.conj(prev))
            if m == "WFM":
                # 75 kHz deviation -> +-1, then de-emphasis (one pole), then 15 kHz low-pass and 48 kHz.
                a = disc * self.if_rate / (2 * np.pi * 75000)
                if self.de_b != 1.0 and len(a):
                    a, zf = lfilter([self.de_b], [1, -self.de_a], a, zi=[self.de_z])
                    self.de_z = zf[0]
                audio = self.audio(a)
            else:
                audio = disc * AUDIO_RATE / (2 * np.pi * max(self.bandwidth / 2 * 0.8, 1500))
        elif m == "AM":
            env = np.abs(y)
            if len(env):                         # the carrier level, followed slowly, is the DC to remove
                mean = float(np.mean(env))
                self._dc = mean if not self._dc else 0.9 * self._dc + 0.1 * mean
            audio = self._gain(env - self._dc)
        elif m in ("USB", "LSB", "CW"):
            audio = self._gain(self.ssb_out(self.ssb(self.ssb_in(y))).real)
        else:
            audio = np.zeros(len(y))
        audio = np.asarray(audio, np.float32)
        if not self.open:
            audio = np.zeros_like(audio)
        return audio

    def _gain(self, x):
        """AGC for AM and SSB: follows the signal's peak, rising at once and falling slowly (half in
        about a second), so pauses between words or Morse elements don't pump the noise up. The
        gain is capped, so pure noise isn't raised to full volume either."""
        if not len(x):
            return x
        peak = float(np.percentile(np.abs(x), 99)) + 1e-12
        release = 0.5 ** (len(x) / AUDIO_RATE / AGC_RELEASE)
        self._peak = peak if peak > self._peak else max(peak, self._peak * release)
        return x * (0.5 / max(self._peak, 0.5 / AGC_MAX_GAIN))


class Channelizer:
    """Several narrow channels out of the dongle's samples at once, from one FFT per block
    (overlap-save, half a block of overlap): each channel takes the m bins around its frequency,
    through its low-pass, and an inverse FFT of m points gives it at 48 kHz directly. Adding
    a channel costs a small inverse FFT, not another filter over millions of samples a second."""

    def __init__(self, rate, m=1024):
        self.rate = int(rate)
        if self.rate % AUDIO_RATE:
            raise ValueError(f"sample rate {rate} isn't a multiple of {AUDIO_RATE}")
        self.d = self.rate // AUDIO_RATE
        self.m = int(m)
        self.n = self.d * self.m                        # FFT size; each block brings n / 2 new samples
        self._buf = np.zeros(self.n // 2, np.complex64)  # the previous half block (the overlap)
        self._new = []
        self._new_n = 0
        self._blocks = 0
        self._filters: dict[float, np.ndarray] = {}
        self.channels: dict = {}                        # key -> {"offset", "k0", "H", "mixer", "last"}

    def _filter(self, width):
        """The low-pass for a channel `width` Hz wide, as the m bins around 0 Hz of its spectrum."""
        H = self._filters.get(width)
        if H is None:
            from scipy.signal import firwin
            taps = min(self.n // 2 + 1, 4 * self.d * 64 + 1)        # short enough for the overlap
            h = firwin(taps, width / 2, fs=self.rate, window=("kaiser", 7.0))
            full = np.fft.fft(h, self.n)
            k = (np.arange(self.m) - self.m // 2) % self.n
            # The filter's delay (taps // 2 samples) is the same for every channel: undone by the
            # discard of the first half of each block, which is longer.
            H = self._filters[width] = (full[k] / self.d).astype(np.complex64)
        return H

    def set(self, key, offset, width=12500):
        """A channel `offset` Hz from the dongle's centre (or a new offset for it)."""
        k0 = int(round(offset / self.rate * self.n))
        old = self.channels.get(key)
        ch = {"offset": float(offset), "k0": k0, "H": self._filter(float(width)), "width": float(width),
              "mixer": Mixer(AUDIO_RATE, offset - k0 * self.rate / self.n),
              "last": old["last"] if old else 1 + 0j}
        self.channels[key] = ch

    def remove(self, key):
        self.channels.pop(key, None)

    def process(self, x) -> dict:
        """Samples since the last call -> {key: complex baseband at 48 kHz} for each channel."""
        out = {k: [] for k in self.channels}
        self._new.append(np.asarray(x, np.complex64))
        self._new_n += len(x)
        half = self.n // 2
        if self._new_n < half:
            return {k: np.zeros(0, np.complex64) for k in out}
        data = np.concatenate(self._new)
        used = (len(data) // half) * half
        rest = data[used:]
        for i in range(0, used, half):
            block = np.concatenate([self._buf, data[i:i + half]])
            self._buf = data[i:i + half]
            X = np.fft.fft(block)
            sign = -1 if self._blocks % 2 else 1
            for key, ch in self.channels.items():
                k = (ch["k0"] + np.arange(self.m) - self.m // 2) % self.n
                y = np.fft.ifft(np.fft.ifftshift(X[k] * ch["H"]))[self.m // 2:]
                if ch["k0"] % 2 and sign < 0:
                    y = -y                               # the bin shift's phase from one block to the next
                out[key].append(y.astype(np.complex64))
            self._blocks += 1
        self._new = [rest] if len(rest) else []
        self._new_n = len(rest)
        # The mixer takes the last few Hz the bin shift couldn't (a fraction of a bin).
        return {k: self.channels[k]["mixer"](np.concatenate(v)) if v else np.zeros(0, np.complex64)
                for k, v in out.items()}


def fm_discriminator(ch, y, deviation=5000.0):
    """A channel's narrow-FM audio, scaled as the receiver's NFM (+-1 at about the deviation)."""
    if not len(y):
        return np.zeros(0, np.float32)
    prev = np.concatenate([[ch["last"]], y[:-1]])
    ch["last"] = y[-1]
    return (np.angle(y * np.conj(prev)) * AUDIO_RATE / (2 * np.pi * deviation)).astype(np.float32)


def find_carriers(power_db, rate, centre, max_width=40000, min_snr=12.0, merge=15000):
    """Narrow carriers well above the noise in an averaged spectrum (FFT-shifted dB): their
    frequencies (Hz), strongest first. The DC spike and the band's edges are left out, and so is
    anything wider than max_width (a broadcast, or noise)."""
    n = len(power_db)
    freqs = centre + (np.arange(n) - n / 2) * rate / n
    floor = float(np.median(power_db))
    hot = power_db > floor + min_snr
    hot &= np.abs(freqs - centre) > 5000                      # the dongle's DC spike
    hot &= np.abs(freqs - centre) < rate * 0.45               # its filter slopes
    found = []
    i = 0
    while i < n:
        if not hot[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and hot[j + 1]:
            j += 1
        if (j - i + 1) * rate / n <= max_width:
            w = 10 ** (power_db[i:j + 1] / 10)
            found.append((float(power_db[i:j + 1].max()), float(np.sum(freqs[i:j + 1] * w) / np.sum(w))))
        i = j + 1
    out = []
    for _, f in sorted(found, reverse=True):
        if all(abs(f - g) > merge for g in out):
            out.append(f)
    return out
