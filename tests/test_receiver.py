"""The Spectrum receiver's signal processing, on synthetic signals."""

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from sdr_toolkit.receiver import dsp  # noqa: E402

RATE = 2400000


def tone_freq(audio, rate=dsp.AUDIO_RATE):
    a = audio[len(audio) // 4:]                      # skip filters settling
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a))))
    spec[:int(100 * len(a) / rate)] = 0              # ignore DC / CTCSS region
    return np.argmax(spec) * rate / len(a)


def blocks(x, n=RATE // 10):
    for i in range(0, len(x), n):
        yield x[i:i + n]


def run(demod, x):
    return np.concatenate([demod.process(b) for b in blocks(x)])


def noise(n, level=0.01, seed=0):
    r = np.random.default_rng(seed)
    return (level * (r.standard_normal(n) + 1j * r.standard_normal(n))).astype(np.complex64)


t = np.arange(RATE) / RATE                            # one second


def test_spectrum_peak_where_the_signal_is():
    x = 0.5 * np.exp(2j * np.pi * 300000 * t[:8192]).astype(np.complex64) + noise(8192)
    s = dsp.Spectrum(4096, average=0)
    db = s.update(x)
    peak = np.argmax(db)
    assert abs((peak - 2048) * RATE / 4096 - 300000) < RATE / 4096 * 1.5
    assert -10 < db[peak] < 0 and np.median(db) < db[peak] - 40


def test_wfm():
    phase = 2 * np.pi * 75000 * 0.5 * np.cumsum(np.sin(2 * np.pi * 1000 * t)) / RATE
    x = (0.5 * np.exp(1j * (2 * np.pi * 200000 * t + phase))).astype(np.complex64) + noise(RATE)
    audio = run(dsp.Demodulator(RATE, "WFM", 200000), x)
    assert len(audio) == dsp.AUDIO_RATE
    assert abs(tone_freq(audio) - 1000) < 5
    assert 0.1 < np.std(audio[len(audio) // 4:]) < 1.0


def test_nfm():
    phase = 2 * np.pi * 2500 * np.cumsum(np.sin(2 * np.pi * 1000 * t)) / RATE
    x = (0.3 * np.exp(1j * (-2 * np.pi * 100000 * t + phase))).astype(np.complex64) + noise(RATE)
    assert abs(tone_freq(run(dsp.Demodulator(RATE, "NFM", -100000), x)) - 1000) < 5


def test_am():
    x = (0.3 * (1 + 0.5 * np.sin(2 * np.pi * 1000 * t)) * np.exp(2j * np.pi * 50000 * t)).astype(np.complex64)
    audio = run(dsp.Demodulator(RATE, "AM", 50000), x + noise(RATE))
    assert abs(tone_freq(audio) - 1000) < 5 and np.max(np.abs(audio[len(audio) // 2:])) < 1.0


@pytest.mark.parametrize("mode,side", [("USB", 1), ("LSB", -1)])
def test_ssb_hears_its_own_sideband_only(mode, side):
    f0 = 150000
    sig = (0.3 * np.exp(2j * np.pi * (f0 + side * 1000) * t)).astype(np.complex64)
    other = (0.3 * np.exp(2j * np.pi * (f0 - side * 1500) * t)).astype(np.complex64)
    audio = run(dsp.Demodulator(RATE, mode, f0), sig + noise(RATE, 0.001))
    assert abs(tone_freq(audio) - 1000) < 5
    wrong = run(dsp.Demodulator(RATE, mode, f0), other + noise(RATE, 0.001))
    a, b = audio[len(audio) // 2:], wrong[len(wrong) // 2:]
    # The other sideband is cut by the filter (AGC lifts what's left, so compare before AGC).
    d1, d2 = dsp.Demodulator(RATE, mode, f0), dsp.Demodulator(RATE, mode, f0)
    d1._gain = d2._gain = lambda v: v
    assert np.std(run(d2, other)[dsp.AUDIO_RATE // 2:]) < 0.05 * np.std(run(d1, sig)[dsp.AUDIO_RATE // 2:])
    assert len(a) and len(b)


def test_cw_tone():
    x = (0.2 * np.exp(2j * np.pi * -80000 * t)).astype(np.complex64) + noise(RATE, 0.001)
    assert abs(tone_freq(run(dsp.Demodulator(RATE, "CW", -80000), x)) - dsp.CW_TONE) < 5


def test_blocks_give_the_same_audio_as_one_go():
    phase = 2 * np.pi * 2500 * np.cumsum(np.sin(2 * np.pi * 700 * t)) / RATE
    x = (0.3 * np.exp(1j * (2 * np.pi * 30000 * t + phase))).astype(np.complex64)
    one = dsp.Demodulator(RATE, "NFM", 30000).process(x)
    many = run(dsp.Demodulator(RATE, "NFM", 30000), x)
    assert len(one) == len(many) and np.max(np.abs(one - many)) < 1e-3       # no clicks at block edges


def test_squelch():
    d = dsp.Demodulator(RATE, "NFM", 0, squelch=-40)
    quiet = run(d, noise(RATE, 0.0005))
    assert not d.open and not np.any(quiet[-1000:])
    loud = run(d, (0.3 * np.exp(2j * np.pi * 1000 * t)).astype(np.complex64))
    assert d.open and np.any(loud[-1000:])


def test_every_rate_and_mode_builds():
    for rate in dsp.RATES:
        for mode in dsp.MODES:
            if mode == "WFM" and rate < 200000:
                continue
            d = dsp.Demodulator(rate, mode, 0)
            out = d.process(noise(rate // 10))
            assert len(out) == dsp.AUDIO_RATE // 10, (rate, mode, len(out))


def test_keeps_up_with_real_time():
    x = noise(RATE, 0.1)
    for mode in ("WFM", "NFM", "USB"):
        d = dsp.Demodulator(RATE, mode, 123000)
        s = dsp.Spectrum(8192)
        t0 = time.perf_counter()
        for b in blocks(x):
            d.process(b)
            s.update(b)
        assert time.perf_counter() - t0 < 0.5, mode          # one second of 2.4 MS/s in under half a second


# ---- the receiver mode -----------------------------------------------------

import subprocess  # noqa: E402

from sdr_toolkit.modes import receiver as R  # noqa: E402


class FakeAudio:
    def __init__(self, *a, **k):
        self.chunks, self.error = [], ""
        FakeAudio.last = self

    def play(self, audio, volume=1.0):
        self.chunks.append(np.asarray(audio) * volume)

    def close(self):
        pass


def fm_station(path, offset=600000, seconds=2.0, rate=RATE):
    n = int(rate * seconds)
    tt = np.arange(n) / rate
    phase = 2 * np.pi * 75000 * 0.5 * np.cumsum(np.sin(2 * np.pi * 1000 * tt)) / rate
    x = 0.5 * np.exp(1j * (2 * np.pi * offset * tt + phase)) + noise(n, 0.02)
    iq = np.empty(2 * n, np.float32)
    iq[0::2], iq[1::2] = x.real, x.imag
    np.clip(iq * 127.5 + 127.5, 0, 255).astype(np.uint8).tofile(path)
    return path


def start_receiver(monkeypatch, **values):
    monkeypatch.setattr(R, "AudioOut", FakeAudio)
    m = R.ReceiverMode()
    v = dict(m.values)
    v.update(values)
    m.start(v)
    return m


def poll_for(m, seconds):
    ev = []
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.05)
        ev += m.poll()
    return ev


def test_receiver_on_a_recording(monkeypatch, tmp_path):
    rec = fm_station(tmp_path / "fm.cu8")
    # 101.1 MHz is tuned a quarter span (600 kHz) right of the centre: where the station is.
    m = start_receiver(monkeypatch, input="file", file=str(rec), file_rate=RATE, freq_mhz=101.1, demod="WFM",
                       bandwidth=180000, volume=100)
    try:
        ev = poll_for(m, 2.0)
        frames = [e.data for e in ev if e.kind == "spectrum"]
        assert len(frames) > 20 and frames[-1]["center"] == 100.5e6 and frames[-1]["rate"] == RATE
        db = frames[-1]["db"]
        assert abs((np.argmax(db) - len(db) / 2) * RATE / len(db) - 600000) < 40000      # within its ±37.5 kHz swing
        audio = np.concatenate(FakeAudio.last.chunks)
        assert abs(tone_freq(audio) - 1000) < 5 and frames[-1]["level"] > -20
        assert any(e.kind == "activity" and "recording" in e.text for e in ev)
        # Clicking 50 kHz along stays within the span: the dongle isn't retuned.
        m.apply(dict(m.values, freq_mhz=101.15))
        assert m.center == 100.5e6 and abs(m.demod.offset - 650000) < 1
        # A frequency outside the span retunes the dongle, the signal again a quarter span off centre.
        m.apply(dict(m.values, freq_mhz=145.0))
        assert m.center == 145e6 - RATE / 4
        m.apply(dict(m.values, demod="NFM", bandwidth=12500, squelch_on=True, squelch=-30))
        assert m.demod.mode == "NFM" and m.demod.bandwidth == 12500 and m.demod.squelch == -30
    finally:
        m.stop()


def test_local_dongle_missing_is_explained(monkeypatch):
    present = any(i in subprocess.run(["lsusb"], capture_output=True, text=True).stdout
                  for i in ("0bda:2838", "0bda:2832"))
    if present:
        pytest.skip("an RTL-SDR is plugged in (it may be in use): not opening it from a test")
    monkeypatch.setattr(R, "AudioOut", FakeAudio)
    m = R.ReceiverMode()
    with pytest.raises(RuntimeError, match="No RTL-SDR found"):
        m.start(dict(m.values, input="rtl"))
    m.stop()


# ---- decoding the receiver's audio --------------------------------------------

from sdr_toolkit.receiver import decode as D  # noqa: E402


@pytest.mark.parametrize("rate", [8000, 12000, 22050, 48000])
def test_resampler_keeps_a_tone_clean_across_blocks(rate):
    n = 48000 * 2
    x = np.sin(2 * np.pi * 1000 * np.arange(n) / 48000).astype(np.float32)
    r = D.Resampler(48000, rate)
    sizes = [4800, 3333, 777, 4801, 9600]
    out, i, k = [], 0, 0
    while i < n:
        out.append(r(x[i:i + sizes[k % len(sizes)]]))
        i += sizes[k % len(sizes)]
        k += 1
    y = np.concatenate(out)
    assert abs(len(y) - n * rate / 48000) <= 2
    assert abs(tone_freq(y, rate) - 1000) < 3
    whole = D.Resampler(48000, rate)(x)
    assert len(whole) == len(y) and np.max(np.abs(whole - y)) < 1e-5   # no clicks where blocks meet
    assert 0.95 < np.max(np.abs(y[len(y) // 2:])) < 1.05                # and the tone's level kept


def save_iq(path, x):
    iq = np.empty(2 * len(x), np.float32)
    iq[0::2], iq[1::2] = x.real, x.imag
    np.clip(iq * 127.5 + 127.5, 0, 255).astype(np.uint8).tofile(path)
    return path


def fm_iq(audio48, offset, deviation, rate=RATE):
    from scipy.signal import resample_poly
    a = resample_poly(audio48, rate // 48000, 1)
    phase = 2 * np.pi * deviation * np.cumsum(a) / rate
    tt = np.arange(len(a)) / rate
    return (0.5 * np.exp(1j * (2 * np.pi * offset * tt + phase))).astype(np.complex64) + noise(len(a), 0.01)


def decoded(ev):
    return [e.data["event"] for e in ev if e.kind == "decoded"]


def run_decoder(monkeypatch, rec, decoder, demod, bandwidth, until, wait=20):
    m = start_receiver(monkeypatch, input="file", file=str(rec), file_rate=RATE, freq_mhz=145.175,
                       demod=demod, bandwidth=bandwidth)
    m.set_decoder(decoder, {})
    ev = []
    end = time.time() + wait
    try:
        while time.time() < end:
            time.sleep(0.1)
            ev += m.poll()
            if until(decoded(ev)):
                break
    finally:
        m.stop()
    return m, ev


def test_aprs_decoded_from_the_receivers_audio(monkeypatch, tmp_path):
    import signals
    audio = signals.aprs_packets(48000)
    audio = audio / np.max(np.abs(audio))
    rec = save_iq(tmp_path / "aprs.cu8", fm_iq(audio, 600000, 3000))     # 145.175 MHz sits 600 kHz up
    found = lambda ev: any("WB2OSZ-15>TEST" in (e.text or "") for e in ev)   # noqa: E731
    m, ev = run_decoder(monkeypatch, rec, "aprs", "NFM", 12500, found)
    assert found(decoded(ev)), [e.text for e in decoded(ev)][-8:]
    assert any(e.kind == "decoder_audio" and e.data["rate"] == 48000 for e in ev)


def keyed_carrier(text, offset, wpm=20):
    """A carrier keyed on and off (5 ms raised-cosine edges, as a transmitter's) with the
    timing of the Morse generator's text."""
    from scipy.signal import hilbert
    from sdr_toolkit.engines.morse.synth import transmission
    a = transmission(text, wpm, None, None, False, False, False, rate=8000, lead_noise_s=0.5)
    e = np.convolve(np.abs(hilbert(a)), np.ones(40) / 40, "same")
    key = (e > 0.5 * e.max()).astype(float)
    w = np.hanning(80)
    key = np.concatenate([np.convolve(key, w / w.sum(), "same"), np.zeros(8000)])
    key = np.interp(np.arange(len(key) * (RATE // 8000)) / (RATE // 8000), np.arange(len(key)), key)
    tt = np.arange(len(key)) / RATE
    return (0.3 * key * np.exp(2j * np.pi * offset * tt)).astype(np.complex64) + noise(len(key), 0.01)


def test_cw_agc_doesnt_pump_up_the_gaps():
    x = keyed_carrier("PARIS", 0)
    d = dsp.Demodulator(RATE, "CW", 0)
    audio = run(d, x)
    gaps = np.abs(audio) < 0.02
    assert np.mean(gaps) > 0.3                                        # the pauses stay quiet
    assert np.max(np.abs(audio)) < 1.0


def test_morse_decoded_from_a_keyed_carrier(monkeypatch, tmp_path):
    x = keyed_carrier("CQ TEST DE VK2ABC", 600000)
    rec = save_iq(tmp_path / "cw.cu8", x)
    found = lambda ev: "VK2ABC" in "".join((e.text or "") for e in ev if e.kind in ("line", "live"))  # noqa: E731
    m, ev = run_decoder(monkeypatch, rec, "morse", "CW", 500, found, wait=25)
    text = "".join((e.text or "") for e in decoded(ev) if e.kind in ("line", "live"))
    assert "VK2ABC" in text, text[-200:]


def test_a_decoder_that_cant_start_says_why(monkeypatch, tmp_path):
    from sdr_toolkit.modes.base import MissingParts
    rec = fm_station(tmp_path / "fm.cu8", seconds=0.5)
    from sdr_toolkit.modes import wsjt

    def missing(self, values):
        raise MissingParts(["wsjtx"])

    monkeypatch.setattr(wsjt.WSJTMode, "start", missing)
    m, ev = run_decoder(monkeypatch, rec, "ft8", "USB", 3000, lambda ev: bool(ev), wait=5)
    warn = [e.text for e in decoded(ev) if e.kind == "line" and e.style == "warn"]
    assert warn and "needs wsjtx" in warn[0] and m.decoder is None


# ---- identifying signals ---------------------------------------------------------

from sdr_toolkit.receiver import identify as I  # noqa: E402

_N = RATE // 2
_t = np.arange(_N) / RATE


def _id_fm(audio, dev, amp=0.3):
    return (amp * np.exp(1j * (2 * np.pi * 200000 * _t + 2 * np.pi * dev * np.cumsum(audio) / RATE))).astype(np.complex64)


def _id_voice(lo, hi, seed=3):
    from scipy.signal import butter, sosfilt
    v = sosfilt(butter(6, [lo, hi], btype="band", fs=RATE, output="sos"), np.random.default_rng(seed).standard_normal(_N))
    return v / np.max(np.abs(v))


def _id_fsk(levels, baud, devs, seed=3):
    sym = np.random.default_rng(seed).integers(0, levels, int(_N / RATE * baud) + 2)
    a = np.repeat(np.array(devs)[sym], int(RATE / baud) + 1)[:_N]
    k = RATE // baud // 3
    return _id_fm(np.convolve(a, np.ones(k) / k, "same"), 1.0)


def _id_signal(kind):
    from scipy.signal import hilbert, resample_poly
    if kind == "wfm":
        return _id_fm(_id_voice(30, 15000) * 0.8, 75000), 101.1
    if kind == "nfm":
        return _id_fm(_id_voice(300, 3000), 2500), 476.5
    if kind == "dmr":
        return _id_fsk(4, 4800, (-1944, -648, 648, 1944)), 460.0
    if kind == "pocsag":
        return _id_fsk(2, 1200, (-4500, 4500)), 155.0
    if kind == "aprs":
        import signals
        a = signals.aprs_packets(48000)
        return _id_fm(resample_poly(a / np.max(np.abs(a)), 50, 1)[:_N], 3000), 145.175
    if kind == "am":
        return (0.3 * (1 + 0.6 * _id_voice(300, 3000)) * np.exp(2j * np.pi * 200000 * _t)).astype(np.complex64), 125.0
    if kind == "usb":
        return (0.3 * hilbert(_id_voice(300, 2700)) * np.exp(2j * np.pi * 200000 * _t)).astype(np.complex64), 14.2
    if kind == "cw":
        return keyed_carrier("CQ CQ CQ", 200000)[RATE:RATE + _N], 7.03
    return np.zeros(_N, np.complex64), 300.0


EXPECT = {"wfm": ("FM broadcast", "WFM", ""), "nfm": ("narrow FM", "NFM", ""), "dmr": ("DMR", "NFM", "dmr"),
          "pocsag": (("POCSAG", "2-level FSK"), "NFM", "pager"), "aprs": ("APRS", "NFM", "aprs"), "am": ("AM", "AM", ""),
          "usb": ("USB", "USB", ""), "cw": ("Morse", "CW", "morse"), "noise": ("nothing", "", "")}


@pytest.mark.parametrize("noise_level", [0.01, 0.3])
@pytest.mark.parametrize("kind", list(EXPECT))
def test_identify(kind, noise_level):
    x, mhz = _id_signal(kind)
    m = I.measure(x + noise(_N, noise_level), RATE, 200000)
    top = I.guesses(m, mhz)[0]
    names, demod, decoder = EXPECT[kind]
    names = (names,) if isinstance(names, str) else names
    assert any(n in top.name for n in names) and top.demod == demod and (not decoder or top.decoder == decoder), (top, m)


def test_band_plan_points_to_the_dedicated_modes():
    m = I.Measurement(snr=20, bandwidth=1500000)
    assert I.guesses(m, 1090.0)[0].mode == "adsb"
    assert any(g.mode == "ais" for g in I.guesses(I.Measurement(snr=20, bandwidth=12000), 161.975))
    assert any(g.decoder == "ft8" for g in I.guesses(I.Measurement(snr=20, bandwidth=2500), 14.074))


# ---- the survey (mega-spectrum) and tags ---------------------------------------------

from sdr_toolkit.receiver import survey as SV  # noqa: E402

STATIONS = (100.3e6, 104.5e6, 433.92e6)           # pretend transmitters (Hz)


def band_samples(center, n, rate=RATE, extra=()):
    """What a dongle tuned to *center* would see: the stations in its span, and noise."""
    tt = np.arange(n) / rate
    x = noise(n, 0.001, seed=int(center) % 1000)
    for f in STATIONS + tuple(extra):
        if abs(f - center) < rate / 2:
            x = x + (0.2 * np.exp(2j * np.pi * (f - center) * tt)).astype(np.complex64)
    return x


def sweep(sv, passes=1, stale=()):
    """Run the survey on simulated samples; *stale* stations appear only during settling."""
    now = 0.0
    c = sv.next_center()
    sv.tuned(c, now)
    done = 0
    while done < passes * len(sv.centers):
        now += 0.01
        old = now - sv.tuned_at < sv.settle
        x = band_samples(sv.current, RATE // 100, extra=stale if old else ())
        if sv.feed(x, now):
            done += 1
            c = sv.next_center()
            sv.tuned(c, now)
    return now


def peaks(db, lo, hi, n=4):
    f = np.linspace(lo, hi, len(db), endpoint=False)
    d = np.nan_to_num(db, nan=-200)
    out = []
    for i in np.argsort(d)[::-1]:
        if all(abs(f[i] - g) > 1e6 for g in out):
            out.append(f[i])
        if len(out) == n:
            break
    return sorted(out), d


def test_survey_stitches_the_band():
    sv = SV.Survey(RATE, tuner=5, settle=0.05)
    sv.set_view(95e6, 440e6)
    assert len(sv.centers) == int(np.ceil(345e6 / (RATE * SV.USABLE)))
    sweep(sv)
    found, d = peaks(sv.stitched(20000), sv.lo, sv.hi, 3)
    for f, want in zip(found, STATIONS):
        assert abs(f - want) < 40000, (found, STATIONS)
    assert np.percentile(d, 50) < -60 and np.max(d) > -25


def test_survey_settle_throws_away_the_old_frequency():
    sv = SV.Survey(RATE, tuner=5, settle=0.05)
    sv.set_view(200e6, 230e6)
    # A ghost that only shows in samples arriving while settling (the previous frequency's).
    ghost = [c + 300000 for c in sv.centers]
    sweep(sv, stale=ghost)
    d = np.nan_to_num(sv.stitched(4000), nan=-200)
    assert np.max(d) < -40                           # nothing but noise in 200-230 MHz


def test_survey_zoomed_in_sweeps_less():
    sv = SV.Survey(RATE, tuner=5)
    whole = len(sv.centers)
    sv.set_view(100e6, 120e6)
    assert len(sv.centers) < whole and len(sv.centers) == int(np.ceil(20e6 / (RATE * SV.USABLE)))
    sv.set_view(100e6, 101e6)                         # inside one slice: the dongle just stays there
    assert sv.centers == [100.5e6]
    assert (sv.min_hz, sv.max_hz) == (24e6, 1766e6)


def test_receiver_surveys_with_retune_commands(monkeypatch, tmp_path):
    from sdr_toolkit.receiver.source import SET_FREQ
    rec = fm_station(tmp_path / "fm.cu8", seconds=0.5)
    m = start_receiver(monkeypatch, input="file", file=str(rec), file_rate=RATE, settle_ms=20)
    try:
        m.start_survey(90e6, 110e6)
        ev = poll_for(m, 3.0)
        frames = [e.data for e in ev if e.kind == "survey"]
        assert frames and frames[-1]["lo"] == 90e6 and frames[-1]["steps"] == int(np.ceil(20e6 / (RATE * 0.8)))
        tuned = {v for c, v in m.src.commands if c == SET_FREQ}
        assert len(tuned) > 3                         # it hopped
        m.stop_survey()
        poll_for(m, 0.5)
        assert m.survey is None and m.src.commands[-1] == (SET_FREQ, int(m.center))
    finally:
        m.stop()


def test_tags(tmp_path):
    from sdr_toolkit.logbook import Logbook
    lb = Logbook(tmp_path / "log.sqlite")
    i = lb.add_tag(438.8e6, "DMR repeater", "NFM", 12500, "dmr")
    lb.add_tag(145.175e6, "APRS", "NFM", 12500, "aprs")
    assert [t["label"] for t in lb.tags(400e6, 500e6)] == ["DMR repeater"]
    assert lb.tags()[0]["decoder"] == "aprs"
    lb.delete_tag(i)
    assert [t["label"] for t in lb.tags()] == ["APRS"]
