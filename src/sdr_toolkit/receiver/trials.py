"""Identifying a signal by decoding it, like Shazam for signal types.

Identify (the button) records LISTEN seconds of the channel around the tuned frequency, at 240
kHz, and puts it through the toolkit's own decoders: DSD-FME (DMR, P25, NXDN, D-Star, YSF),
multimon-ng (POCSAG and FLEX pagers), direwolf (APRS, and AIS near the marine band), the
CTCSS / DCS / DTMF decoder, rtl_433 in the ISM bands, and for broadcast FM a look for the 19 kHz
stereo pilot and the 57 kHz RDS carrier. Whatever decodes is the answer, with what it decoded as
proof; the measured guesses (identify.py) come after it, as "looks like".
"""

import logging
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .identify import Guess

log = logging.getLogger(__name__)
LISTEN = 8.0            # s recorded when Identify is pressed
RATE = 240000           # the recording's sample rate (from rates that are a multiple of it)
WAIT = 25.0             # s a decoder gets for the recording, at most


def capture_rate(rate) -> int:
    return RATE if rate % RATE == 0 else int(rate)


def _mode(mode_id):
    from ..modes import MODES
    return next(M for M in MODES if M.id == mode_id)()


def _run_file(mode_id, path, rate, **values):
    """A mode that reads recordings itself (DSD-FME, direwolf for AIS, rtl_433), run on the snippet."""
    m = _mode(mode_id)
    v = dict(m.values)
    v.update(values, input="file", file=str(path), file_rate=rate)
    m.start(v)
    events = []
    end = time.monotonic() + WAIT
    try:
        while time.monotonic() < end:
            time.sleep(0.2)
            events += m.poll()
            if m._proc.poll() is not None and m._lines.empty():
                break
        time.sleep(0.3)
        events += m.poll()
    finally:
        events += m.stop()
    return events


def _run_audio(mode_id, audio48, **values):
    """A mode that decodes audio (multimon-ng, direwolf for APRS, the tones decoder), fed the
    snippet's FM audio as fast as it takes it."""
    from .decode import Resampler
    m = _mode(mode_id)
    v = dict(m.values)
    v.update(values)
    m.start(v)
    x = Resampler(48000, m.rate)(np.concatenate([audio48, np.zeros(48000, np.float32)]))
    events = []
    t0 = time.time()
    n = max(int(m.rate * 0.1), 1)
    try:
        for i in range(0, len(x), n):
            events += list(m.feed(x[i:i + n], t0 + i / m.rate))
            events += list(m.poll())
        end = time.monotonic() + 3.0
        while time.monotonic() < end:           # outside programs finish what they were given
            time.sleep(0.2)
            events += list(m.poll())
    finally:
        events += list(m.stop())
    return events


def _texts(events, kinds=("line", "log")):
    return [e.text for e in events if e.kind in kinds and e.text and e.style not in ("warn", "dim")]


# ---- what counts as decoded, per decoder ------------------------------------------------------

def _dmr(path, rate, audio):
    events = _run_file("dmr", path, rate, voice=False, file_offset=0)
    heard = [t for t in _texts(events) if " signal" in t or " ended: " in t]
    calls = [t for t in _texts(events) if t not in heard]
    if not heard and not calls:
        return None
    first = (heard or calls)[0].split("  ", 1)[-1]
    proto = first.split()[0]
    why = first + (f"; {len(calls)} call events" if calls else "")
    return Guess(f"{proto} digital radio (decoded)", 1.0, f"DSD-FME: {why}", "NFM", 12500, "dmr")


def _pager(path, rate, audio):
    rows = [e.data for e in _run_audio("pager", audio) if e.kind == "row"]
    if not rows:
        return None
    r = rows[0]
    return Guess(f"{r.get('proto', 'Pager')} pager (decoded)", 1.0,
                 f"multimon-ng: {len(rows)} message(s), e.g. address {r.get('address', '?')}: "
                 f"{str(r.get('message', ''))[:60]}", "NFM", 12500, "pager")


def _aprs(path, rate, audio):
    packets = [t for t in _texts(_run_audio("aprs", audio)) if ">" in t]
    if not packets:
        return None
    return Guess("APRS / AX.25 packet (decoded)", 1.0,
                 f"direwolf: {len(packets)} packet(s), e.g. {packets[0].split('  ', 1)[-1][:70]}", "NFM", 12500, "aprs")


def _tones(path, rate, audio):
    found = [t.split("  ", 1)[-1] for t in _texts(_run_audio("tones", audio))
             if ("CTCSS" in t or "DCS" in t or t.strip().isdigit() or "DTMF" in t) and "gone" not in t]
    if not found:
        return None
    return Guess(f"Analogue FM with {found[0]} (decoded)", 1.0, f"tones decoder: {', '.join(dict.fromkeys(found))}",
                 "NFM", 12500, "tones")


def _ais(path, rate, audio):
    reports = [t for t in _texts(_run_file("ais", path, rate, file_offset=0))]
    if not reports:
        return None
    return Guess("AIS ship transponder (decoded)", 1.0, f"direwolf: {len(reports)} report(s), e.g. {reports[0][:70]}",
                 mode="ais")


def _ism(path, rate, audio):
    rows = [e.data for e in _run_file("ism", path, rate, rate=f"{rate // 1000}k") if e.kind == "row"]
    if not rows:
        return None
    r = rows[0]
    return Guess(f"{r.get('model', 'ISM device')} (decoded)", 1.0,
                 f"rtl_433: {len(rows)} transmission(s), e.g. {r.get('model', '')} {str(r.get('data', ''))[:60]}",
                 mode="ism")


def broadcast_fm(iq, rate) -> Guess | None:
    """FM broadcast proven by its multiplex: the 19 kHz stereo pilot (a pure tone, far above its
    surroundings) and the 57 kHz RDS carrier, in the FM-demodulated baseband."""
    if rate < 150000 or len(iq) < rate:
        return None
    disc = np.angle(iq[1:] * np.conj(iq[:-1]))
    n = 1 << 16
    segs = [disc[i:i + n] for i in range(0, len(disc) - n, n)][:8]
    if not segs:
        return None
    w = np.hanning(n)
    p = np.mean([np.abs(np.fft.rfft(s * w)) ** 2 for s in segs], axis=0)
    f = np.fft.rfftfreq(n, 1 / rate)

    def stand_out(hz, half=150, around=(1500, 4000)):
        line = p[(f > hz - half) & (f < hz + half)].max()
        side = p[((f > hz - around[1]) & (f < hz - around[0])) | ((f > hz + around[0]) & (f < hz + around[1]))]
        return 10 * np.log10(line / (np.median(side) + 1e-30))

    pilot = stand_out(19000)
    if pilot < 15:
        return None
    band = lambda a, b: p[(f > a) & (f < b)].mean()  # noqa: E731
    rds = 10 * np.log10(band(55600, 58400) / (band(60500, 63500) + 1e-30)) > 6
    why = f"19 kHz stereo pilot {pilot:.0f} dB above the noise around it" + ("; RDS at 57 kHz" if rds else "")
    return Guess("FM broadcast, stereo" + (" with RDS" if rds else "") + " (verified)", 1.0, why, "WFM", 180000)


def trials_for(mhz, width):
    """Which decoders are worth a try here."""
    narrow = width < 40000
    out = []
    if narrow:
        out += [("DSD-FME (DMR, P25...)", _dmr, "dsd-fme"), ("multimon-ng (pagers)", _pager, "multimon-ng"),
                ("direwolf (APRS)", _aprs, "direwolf"), ("CTCSS / DCS / DTMF", _tones, None)]
        if 156 <= mhz <= 163:
            out.append(("direwolf (AIS)", _ais, "direwolf"))
        if 300 <= mhz <= 930:
            out.append(("rtl_433 (ISM devices)", _ism, "rtl-433"))
    return out


def run(iq, rate, mhz, width, progress=None) -> tuple[list[Guess], list[str]]:
    """The decoded identifications of the recording (best first), and which decoders were tried."""
    from .. import deps
    from ..modes.radio import write_cu8
    found, tried = [], []
    fm = broadcast_fm(iq, rate) if (width >= 40000 or 76 <= mhz <= 110) else None
    if fm is not None:
        return [fm], ["FM multiplex (pilot, RDS)"]
    trials = [(name, fn) for name, fn, part in trials_for(mhz, width) if part is None or not deps.missing([part])]
    if not trials:
        return [], []
    folder = Path(tempfile.mkdtemp(prefix="sdr-toolkit-identify-"))
    try:
        path = folder / "channel.cu8"
        write_cu8(path, iq)
        from ..modes.radio import fm_audio_chunks
        pcm = b"".join(fm_audio_chunks(path, rate, 0, 48000, 12500, level=0.5))
        audio = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
        lock = threading.Lock()

        def one(item):
            name, fn = item
            try:
                g = fn(path, rate, audio)
            except Exception:  # a decoder that can't run here is just not an answer
                log.exception("identify: %s", name)
                g = None
            with lock:
                tried.append(name)
                if g is not None:
                    found.append(g)
                if progress:
                    progress(len(tried), len(trials))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, trials))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return found, tried
