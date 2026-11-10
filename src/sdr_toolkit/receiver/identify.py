"""Guessing what a signal is, from its shape and where it is.

``measure()`` looks at the channel around the tuned frequency: how wide it is, whether its
envelope is constant (FM, FSK) or varies (AM, SSB), how many frequency levels it hops between
(2-level or 4-level FSK), its symbol rate, whether it's continuous or comes in bursts, and
for narrow FM whether the audio carries AFSK tones (packet). ``guesses()`` combines that
with the band plan (Australian where it differs) into ranked suggestions, each with what to
do: the demodulator and bandwidth to listen with, a decoder to try, or a dedicated mode.
These are informed guesses, and say so.
"""

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import butter, sosfiltfilt

# (low MHz, high MHz, name, demod, bandwidth Hz, decoder or "", dedicated mode or "")
BANDS = (
    (87.5, 108.0, "FM broadcast", "WFM", 180000, "", ""),
    (108.0, 117.975, "aviation navigation (VOR/ILS)", "AM", 10000, "", ""),
    (118.0, 137.0, "airband voice", "AM", 8330, "", ""),
    (137.0, 138.0, "weather satellites (NOAA APT)", "WFM", 40000, "", ""),
    (144.0, 148.0, "2 m amateur", "NFM", 12500, "", ""),
    (145.17, 145.18, "APRS (Australia)", "NFM", 12500, "aprs", ""),
    (144.385, 144.395, "APRS (North America)", "NFM", 12500, "aprs", ""),
    (144.795, 144.805, "APRS (Europe)", "NFM", 12500, "aprs", ""),
    (148.0, 174.0, "VHF land mobile (business, pagers)", "NFM", 12500, "", ""),
    (156.0, 162.025, "marine VHF", "NFM", 25000, "", ""),
    (161.970, 161.980, "AIS channel 1", "NFM", 25000, "", "ais"),
    (162.020, 162.030, "AIS channel 2", "NFM", 25000, "", "ais"),
    (162.4, 162.55, "NOAA weather radio (US)", "NFM", 12500, "", ""),
    (380.0, 400.0, "government trunked radio (often P25 / TETRA, encrypted)", "NFM", 12500, "dmr", ""),
    (400.0, 420.0, "land mobile (business, government)", "NFM", 12500, "", ""),
    (420.0, 450.0, "70 cm amateur", "NFM", 12500, "", ""),
    (433.05, 434.79, "ISM 433 MHz (sensors, remotes)", "NFM", 25000, "", "ism"),
    (446.0, 446.2, "PMR446", "NFM", 12500, "", ""),
    (450.0, 520.0, "land mobile (business DMR, P25, NXDN, analog)", "NFM", 12500, "", ""),
    (476.4, 477.4125, "UHF CB (Australia)", "NFM", 12500, "", ""),
    (915.0, 928.0, "LoRa / ISM 915-928 (Australia, Meshtastic)", "NFM", 250000, "", "lora"),
    (868.0, 868.6, "LoRa / ISM 868 (Europe)", "NFM", 125000, "", "lora"),
    (977.0, 979.0, "UAT (US aircraft)", "NFM", 1000000, "", ""),
    (1089.0, 1091.0, "ADS-B / Mode S aircraft", "NFM", 2000000, "", "adsb"),
    (1574.0, 1577.0, "GPS L1", "NFM", 2000000, "", "gps"),
)
FT8 = (1.840, 3.573, 5.357, 7.074, 10.136, 14.074, 18.100, 21.074, 24.915, 28.074, 50.313, 144.174)
FT4 = (3.575, 7.0475, 10.140, 14.080, 18.104, 21.140, 24.919, 28.180, 50.318)


def bands_at(mhz) -> list:
    return [b for b in BANDS if b[0] <= mhz <= b[1]]


@dataclass
class Measurement:
    level: float = -120.0          # channel power, dBFS
    snr: float = 0.0               # above the span's noise floor, dB
    bandwidth: float = 0.0         # occupied (-20 dB) width, Hz
    centre_offset: float = 0.0     # where the energy's middle is, Hz from the tuned frequency
    asym: float = 0.0              # energy above minus below the tuned frequency, -1..1
    envelope_cv: float = 0.0       # std/mean of the envelope: ~0 constant (FM/FSK), >0.3 AM/SSB
    duty: float = 0.0              # share of time the signal is on
    levels: int = 0                # FSK frequency levels found (2, 4), 0 = none / analog
    baud: float = 0.0              # symbol rate, if there's a clear one
    deviation: float = 0.0         # peak frequency swing, Hz
    afsk: bool = False             # 1200/2200 Hz tones in the FM audio (packet)
    carrier: bool = False          # a strong line at the tuned frequency (AM, CW)
    notes: list = field(default_factory=list)


@dataclass
class Guess:
    name: str
    confidence: float              # 0..1
    why: str
    demod: str = ""
    bandwidth: int = 0
    decoder: str = ""              # an audio decoder to try on the receiver
    mode: str = ""                 # a dedicated mode to open instead


def _channel(x, rate, offset, width):
    """The signal around *offset*, shifted to 0 Hz and decimated to about 4x *width*."""
    n = np.arange(len(x))
    y = x * np.exp(-2j * np.pi * offset * n / rate)
    target = max(4 * width, 24000)
    d = max(1, int(rate // target))
    if d > 1:
        sos = butter(6, min(2 * width, rate / d / 2 * 0.9), fs=rate, output="sos")
        y = sosfiltfilt(sos, y)[::d]
    return y.astype(np.complex64), rate / d


def _occupied(y, fs):
    """Occupied width (10 dB over the noise, spreading out from the strongest point), the middle
    of that energy, its above/below balance and the peak-to-noise ratio, from an averaged spectrum."""
    n = 2048 if len(y) >= 2048 else 1 << int(np.log2(max(len(y), 64)))
    segs = len(y) // n
    p = np.zeros(n)
    w = np.hanning(n)
    for i in range(max(segs, 1)):
        seg = y[i * n:(i + 1) * n]
        if len(seg) < n:
            break
        p += np.abs(np.fft.fftshift(np.fft.fft(seg * w))) ** 2
    p = np.convolve(p, np.ones(5) / 5, "same")
    f = (np.arange(n) - n / 2) * fs / n
    floor = np.percentile(p, 20) + 1e-30
    k = int(np.argmax(p))
    snr = float(10 * np.log10(p[k] / floor))
    over = p > 10 * floor
    gap = max(2, n // 100)                        # small dips inside a signal don't end it
    lo = hi = k
    miss = 0
    while lo > 0 and miss <= gap:
        lo -= 1
        miss = 0 if over[lo] else miss + 1
    lo += miss
    miss = 0
    while hi < n - 1 and miss <= gap:
        hi += 1
        miss = 0 if over[hi] else miss + 1
    hi -= miss
    if snr < 6:
        return 0.0, 0.0, 0.0, snr
    sig = np.clip(p[lo:hi + 1] - floor, 0, None)
    fs_ = f[lo:hi + 1]
    width = (hi - lo + 1) * fs / n
    centre = float(np.sum(fs_ * sig) / max(np.sum(sig), 1e-30))
    asym = float((sig[fs_ > 0].sum() - sig[fs_ < 0].sum()) / max(sig.sum(), 1e-30))
    return width, centre, asym, snr


def _levels(freq, on):
    """How many frequency levels an FSK signal sits on: peaks in the histogram of its frequency."""
    f = freq[on]
    if len(f) < 200:
        return 0
    lo, hi = np.percentile(f, 1), np.percentile(f, 99)
    if hi - lo <= 0:
        return 0
    h, _ = np.histogram(f, bins=60, range=(lo, hi))
    h = np.convolve(h, np.ones(3) / 3, "same")
    peaks = [i for i in range(1, 59) if h[i] > h[i - 1] and h[i] >= h[i + 1] and h[i] > 0.25 * h.max()]
    merged = []
    for p in peaks:
        if not merged or p - merged[-1] > 4:
            merged.append(p)
    if len(merged) not in (2, 4):
        return 0
    # FSK sits on its levels with a clear dip between them; analog FM (voice) makes a lumpy hump.
    deep = 0.45 if len(merged) == 2 else 0.8
    for a, b in zip(merged, merged[1:]):
        if h[a:b + 1].min() > deep * min(h[a], h[b]):
            return 0
    return len(merged)


def _baud(freq, fs):
    """The symbol rate: the strongest line in the spectrum of how fast the frequency changes."""
    if len(freq) < 4096:
        return 0.0
    d = np.abs(np.diff(freq))
    d = d - d.mean()
    spec = np.abs(np.fft.rfft(d * np.hanning(len(d))))
    f = np.fft.rfftfreq(len(d), 1 / fs)
    band = (f > 150) & (f < min(20000, fs / 2.5))
    if not band.any():
        return 0.0
    i = np.argmax(np.where(band, spec, 0))
    if spec[i] < 6 * np.median(spec[band]):
        return 0.0
    return float(f[i])


def measure(x: np.ndarray, rate: float, offset: float, span_width=300000) -> Measurement:
    """Measurements of the signal *offset* Hz from the centre of complex samples *x* at *rate*."""
    m = Measurement()
    if len(x) < 4096:
        return m
    wide, fsw = _channel(x, rate, offset, min(span_width, rate / 2.2) / 2)
    m.level = float(10 * np.log10(np.mean(np.abs(wide) ** 2) + 1e-20))
    m.bandwidth, m.centre_offset, m.asym, m.snr = _occupied(wide, fsw)
    width = max(m.bandwidth, 2000)
    y, fs = _channel(x, rate, offset + m.centre_offset, width / 2 * 1.2)
    env = np.abs(y)
    on = env > 0.3 * np.percentile(env, 95)
    m.duty = float(np.mean(on))
    if on.sum() > 100:
        m.envelope_cv = float(np.std(env[on]) / (np.mean(env[on]) + 1e-12))
    freq = np.angle(y[1:] * np.conj(y[:-1])) * fs / (2 * np.pi)
    on1 = on[1:] & on[:-1]
    if on1.sum() > 200:
        m.deviation = float(np.percentile(np.abs(freq[on1] - np.median(freq[on1])), 98))
        if m.envelope_cv < 0.25:
            k = max(1, int(fs / 20000))
            m.baud = _baud(np.convolve(freq, np.ones(k) / k, "same"), fs)
            # Average over a third of a symbol before counting levels: that takes most of the noise out.
            k = max(k, int(fs / m.baud / 3)) if m.baud else k
            m.levels = _levels(np.convolve(freq, np.ones(k) / k, "same"), on1)
    # A carrier: one narrow line holding most of the power near the tuned frequency.
    spec = np.abs(np.fft.fftshift(np.fft.fft(y[: 1 << 14] * np.hanning(min(len(y), 1 << 14))))) ** 2
    m.carrier = bool(spec.max() > 0.3 * spec.sum())
    # Packet: 1200 and 2200 Hz tones in the FM audio.
    if m.envelope_cv < 0.25 and 6000 < m.bandwidth < 25000:
        audio = freq - np.mean(freq)
        a = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
        af = np.fft.rfftfreq(len(audio), 1 / fs)

        def tone(hz):
            band = (af > hz - 60) & (af < hz + 60)
            ref = (af > 300) & (af < 3500)
            return a[band].max() > 4 * np.median(a[ref]) if band.any() and ref.any() else False

        m.afsk = bool(tone(1200) and tone(2200))
    return m


def _snap(baud, choices, tol=0.12):
    for c in choices:
        if abs(baud - c) <= tol * c:
            return c
    return 0


def guesses(m: Measurement, mhz: float) -> list[Guess]:
    """Ranked guesses for a measured signal at *mhz*."""
    out: list[Guess] = []
    bands = bands_at(mhz)
    band_names = ", ".join(b[2] for b in bands)
    if m.snr < 6:
        out.append(Guess("nothing much here", 0.6, f"the channel is only {m.snr:.0f} dB above the noise"))
    bw, cv = m.bandwidth, m.envelope_cv
    # FM-like: a constant envelope, or (with noise roughening the envelope) a large, symmetric
    # frequency swing with no steady carrier.
    const = cv < 0.25 or (cv < 0.6 and abs(m.asym) < 0.3 and m.deviation > 1000 and not m.carrier)
    desc = f"{bw / 1000:.1f} kHz wide, {'constant' if const else 'varying'} envelope"
    am_like = m.carrier and m.deviation < 300           # a carrier that doesn't move: AM or CW, not FM
    for b in bands:
        if b[6]:
            out.append(Guess(b[2], 0.55 + (0.2 if m.snr > 10 else 0), f"{mhz:.3f} MHz is in the {b[2]} band; its "
                             f"own mode decodes it", b[3], b[4], "", b[6]))
    if const and (50000 < bw < 260000 or m.deviation > 15000):       # weak: only its middle shows, but it swings wide
        conf = 0.95 if any(b[2] == "FM broadcast" for b in bands) else 0.6
        out.append(Guess("FM broadcast (wide FM)", conf, desc, "WFM", 180000))
    if m.levels == 4 and 4000 < bw < 30000:
        rate = _snap(m.baud, (4800, 2400)) or m.baud
        if rate == 2400 or bw < 8000:
            out.append(Guess("NXDN48 / dPMR (4-level FSK, 2400 Bd)", 0.6, f"{desc}, 4 frequency levels, "
                             f"{m.baud:.0f} Bd", "NFM", 12500, "dmr"))
        else:
            burst = m.duty < 0.85
            name = "DMR (4-level FSK, 4800 Bd, TDMA bursts)" if burst else "P25 Phase 1 / DMR (4-level FSK, 4800 Bd)"
            out.append(Guess(name, 0.7, f"{desc}, {m.levels or 'probably 4'} frequency levels, {m.baud:.0f} Bd"
                             + (", in bursts" if burst else ", continuous"), "NFM", 12500, "dmr"))
    digital = _snap(m.baud, (4800, 2400))
    if not m.levels and digital and const and not am_like and not m.afsk and 5000 < bw < 30000:
        out.append(Guess(f"digital voice or data ({digital} Bd), probably {'DMR / P25' if digital == 4800 else 'NXDN / dPMR'}",
                         0.55, f"{desc}, a clear {m.baud:.0f} Bd symbol rate (its levels didn't separate in "
                         "the noise)", "NFM", 12500, "dmr"))
    if m.levels == 2 and const and not am_like:
        pager = _snap(m.baud, (512, 1200, 2400))
        flex = _snap(m.baud, (1600, 3200))
        ais = _snap(m.baud, (9600,))
        if pager:
            out.append(Guess(f"POCSAG pager ({pager} Bd, 2-level FSK)", 0.7, f"{desc}, 2 levels, {m.baud:.0f} Bd, "
                             f"±{m.deviation / 1000:.1f} kHz", "NFM", 12500, "pager"))
        elif flex:
            out.append(Guess(f"FLEX pager ({flex} Bd)", 0.6, f"{desc}, 2 levels, {m.baud:.0f} Bd", "NFM", 12500,
                             "pager"))
        elif ais:
            out.append(Guess("AIS / GMSK data (9600 Bd)", 0.6, f"{desc}, 2 levels, {m.baud:.0f} Bd", "NFM", 25000,
                             "", "ais"))
        else:
            out.append(Guess("2-level FSK data", 0.4, f"{desc}, 2 levels" + (f", {m.baud:.0f} Bd" if m.baud else ""),
                             "NFM", 12500, "pager"))
    if m.afsk:
        out.append(Guess("AFSK packet / APRS (1200 Bd)", 0.85, f"{desc}, 1200 and 2200 Hz tones in the FM audio",
                         "NFM", 12500, "aprs"))
    if const and not am_like and 3000 < bw < 30000 and m.deviation < 15000 and not m.levels and not m.afsk:
        out.append(Guess("narrow FM (voice or tones)", 0.5, desc + ", no data pattern found", "NFM",
                         12500 if bw < 16000 else 25000, "tones"))
    keyed = m.duty < 0.85
    if am_like and not keyed and 1500 < bw < 20000 and abs(m.asym) < 0.4:
        conf = 0.85 if any("airband" in b[2] for b in bands) else 0.6
        out.append(Guess("AM (voice)", conf, f"{desc}, a carrier in the middle, both sidebands", "AM",
                         8330 if conf > 0.8 else 10000))
    if not const and not m.carrier and 500 < bw < 8000 and abs(m.asym) > 0.4:
        side = "USB" if m.asym > 0 else "LSB"
        out.append(Guess(f"single sideband ({side})", 0.6, f"{desc}, energy on one side, no carrier", side, 2700))
    if am_like and (bw <= 1500 or keyed) and m.snr > 6:
        out.append(Guess("Morse (CW)" if keyed else "a plain carrier", 0.75 if keyed else 0.5,
                         "a carrier keyed on and off" if keyed else f"a narrow line ({bw:.0f} Hz), not keyed",
                         "CW", 500, "morse"))
    for f in FT8:
        if abs(mhz - f) < 0.004:
            out.append(Guess("FT8 (the band's FT8 frequency)", 0.75, f"{f:.3f} MHz is the FT8 calling frequency",
                             "USB", 3000, "ft8"))
    for f in FT4:
        if abs(mhz - f) < 0.004:
            out.append(Guess("FT4", 0.7, f"{f:.3f} MHz is the FT4 frequency", "USB", 3000, "ft4"))
    if not out:
        out.append(Guess("unknown", 0.2, desc + (f" ({band_names})" if band_names else "")))
    out.sort(key=lambda g: -g.confidence)
    if band_names:
        for g in out:
            if "band" not in g.why:
                g.why += f"; band: {band_names}"
    return out
