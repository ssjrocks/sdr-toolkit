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
