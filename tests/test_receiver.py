"""The Spectrum receiver's signal processing, on synthetic signals."""

import os
import shutil
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


def poll_for(m, seconds, until=None):
    ev = []
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.05)
        ev += m.poll()
        if until is not None and until(ev):
            break
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
        # Right up to the edge of the span (101.7 MHz, less half the channel) it isn't either.
        m.apply(dict(m.values, freq_mhz=101.6))
        assert m.center == 100.5e6 and abs(m.demod.offset - 1100000) < 1
        m.apply(dict(m.values, freq_mhz=99.4))
        assert m.center == 100.5e6
        # In view but in the edge (101.65 MHz: the 180 kHz channel would stick out): the dongle moves
        # only far enough for it to fit, so the band barely shifts on screen (it used to jump 1.75 MHz).
        m.apply(dict(m.values, freq_mhz=101.65))
        assert 0 < m.center - 100.5e6 < 150000 and abs(m.demod.offset) + 90000 <= RATE / 2
        c = m.center
        # On the DC spike: just clear of it.
        m.apply(dict(m.values, freq_mhz=round(c / 1e6, 6)))
        assert 0 < c - m.center <= 92000 and abs(m.demod.offset) > 2000
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


FAKE_RTL_TCP = r"""#!/usr/bin/env python3
# Behaves like Ubuntu's rtl_tcp 2.0.2 to a pipe: device lines on stderr (unbuffered), and
# "listening..." on stdout, which stays in its buffer until it exits.
import socket, struct, sys, time
args = sys.argv[1:]
port = int(args[args.index("-p") + 1])
sys.stderr.write("Found 1 device(s):\nTuned to 100000000 Hz.\n")
sys.stderr.flush()
time.sleep(0.5)                                        # opening the dongle
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port)); srv.listen(1)
print("listening...")                                  # not flushed
conn, _ = srv.accept()
conn.sendall(b"RTL0" + struct.pack(">II", 6, 29))
try:
    while True:
        conn.sendall(bytes([127, 128]) * 48000)
        time.sleep(0.02)
except OSError:
    pass
"""


def test_local_rtl_tcp_is_ready_when_its_port_answers(monkeypatch, tmp_path):
    """Its "listening..." line sits in a pipe buffer: waiting for it timed out after 10 s and the
    dongle never opened (the error showed rtl_tcp's OsmoSDR help lines)."""
    if sys.platform == "win32":
        pytest.skip("the stand-in rtl_tcp is a script run by its #! line")
    from sdr_toolkit.receiver import source
    exe = tmp_path / "rtl_tcp"
    exe.write_text(FAKE_RTL_TCP)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    t = time.time()
    src = source.LocalRtlTcp("0", 2400000, 100e6)
    try:
        assert time.time() - t < 5
        assert src.tuner_name and src.proc.poll() is None
        time.sleep(0.3)
        assert len(src.read()) > 0
    finally:
        src.close()
    assert src.proc.poll() is not None


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


def test_dmr_heard_through_the_receiver(monkeypatch, tmp_path):
    from sdr_toolkit.modes.digital_voice import dsd_fme
    if dsd_fme() is None:
        pytest.skip("DSD-FME isn't built")
    import signals
    rec = save_iq(tmp_path / "dmr.cu8", fm_iq(signals.dmr_bursts(48000, 3.0), 600000, 1944))
    found = lambda ev: any("  DMR " in (e.text or "") and "colour code" in (e.text or "") for e in ev)   # noqa: E731
    m, ev = run_decoder(monkeypatch, rec, "dmr", "NFM", 12500, found, wait=30)
    heard = [e.text for e in decoded(ev) if "  DMR " in (e.text or "") and "colour code" in (e.text or "")]
    assert heard and "colour code 12" in heard[0], [e.text for e in decoded(ev)][-8:]


def test_receiver_voice_volume_reaches_the_decoder(monkeypatch, tmp_path):
    from sdr_toolkit.modes import digital_voice as dv
    got = []
    monkeypatch.setattr(dv.DigitalVoiceMode, "set_voice_volume", lambda self, pct: got.append(pct))
    monkeypatch.setattr(dv.DigitalVoiceMode, "start", lambda self, values: setattr(self, "values", dict(values))
                        or setattr(self, "rate", 48000))
    monkeypatch.setattr(dv.DigitalVoiceMode, "stop", lambda self: [])
    monkeypatch.setattr(dv.DigitalVoiceMode, "poll", lambda self: [])
    rec = fm_station(tmp_path / "fm.cu8", seconds=0.5)
    m = start_receiver(monkeypatch, input="file", file=str(rec), file_rate=RATE, voice_volume=60)
    try:
        m.set_decoder("dmr", {})
        poll_for(m, 0.5)
        assert m.decoder is not None and m.decoder.mode.values["voice_volume"] == 60
        m.apply(dict(m.values, voice_volume=25))
        poll_for(m, 0.5)
        assert got == [25]
    finally:
        m.stop()


# ---- Identify by decoding (trials.py) ------------------------------------------------------------

def _channel(audio, rate_in, deviation, seconds=8.0, seed=0):
    """A 240 kHz channel recording, FM-modulated with audio (padded to `seconds`), as Identify makes."""
    from scipy.signal import resample_poly
    a = resample_poly(np.asarray(audio, float) / max(np.max(np.abs(audio)), 1e-9), 48000, rate_in)
    a = np.concatenate([a, np.zeros(max(int(48000 * seconds) - len(a), 0))])[: int(48000 * seconds)]
    x = fm_iq(a.astype(np.float32), 0, deviation, rate=240000)
    return x.astype(np.complex64)


def _identified(x, mhz, width):
    from sdr_toolkit.receiver import trials
    found, tried = trials.run(x, 240000, mhz, width)
    return [g.name for g in found], tried


def test_identify_decodes_dmr():
    from sdr_toolkit.modes.digital_voice import dsd_fme
    if dsd_fme() is None:
        pytest.skip("DSD-FME isn't built")
    import signals
    names, tried = _identified(_channel(signals.dmr_bursts(48000, 6.0), 48000, 1944), 438.8, 9000)
    assert any(n.startswith("DMR ") and n.endswith("(decoded)") for n in names), (names, tried)


needs_decoders = pytest.mark.skipif(not (shutil.which("multimon-ng") and shutil.which("direwolf")),
                                    reason="needs multimon-ng and direwolf, not installed here")


@needs_decoders
def test_identify_decodes_a_pager_aprs_and_ctcss():
    import signals
    a = np.concatenate([np.zeros(11025), signals.pocsag(1234567, "HELLO PAGER 42", 1200)])
    names, tried = _identified(_channel(a, 22050, 4500), 148.8, 12000)
    assert "POCSAG1200 pager (decoded)" in names, (names, tried)
    names, _ = _identified(_channel(signals.aprs_packets(48000), 48000, 3000), 145.175, 12000)
    assert "APRS / AX.25 packet (decoded)" in names, names
    names, _ = _identified(_channel(signals.ctcss_voice(88.5, 6), 8000, 2500), 476.5, 9000)
    assert any(n.startswith("Analogue FM with CTCSS 88.5 Hz") for n in names), names


def test_identify_broadcast_fm_by_its_pilot():
    from sdr_toolkit.receiver import trials
    r = 240000
    tt = np.arange(int(r * 3)) / r
    for pilot in (True, False):
        mpx = 0.9 * np.sin(2 * np.pi * 1000 * tt) + (0.1 * np.sin(2 * np.pi * 19000 * tt) if pilot else 0)
        x = (0.5 * np.exp(1j * 2 * np.pi * 75000 * np.cumsum(mpx) / r)).astype(np.complex64) + noise(len(tt), 0.01)
        g = trials.broadcast_fm(x, r)
        assert (g is not None and g.name.startswith("FM broadcast, stereo")) == pilot


@needs_decoders
def test_identify_decodes_nothing_in_noise():
    names, tried = _identified(noise(240000 * 8, 0.1).astype(np.complex64), 460.0, 12000)
    assert names == [] and "multimon-ng (pagers)" in tried


@needs_decoders
def test_receiver_identify_listens_then_decodes(monkeypatch, tmp_path):
    import signals
    a = np.concatenate([np.zeros(11025), signals.pocsag(1234567, "HELLO PAGER 42", 1200), np.zeros(22050 * 9)])
    from scipy.signal import resample_poly
    audio = resample_poly(a / np.max(np.abs(a)), 48000, 22050).astype(np.float32)
    rec = save_iq(tmp_path / "pager.cu8", fm_iq(audio, 600000, 4500))
    m = start_receiver(monkeypatch, input="file", file=str(rec), file_rate=RATE, freq_mhz=145.175, demod="NFM",
                       bandwidth=12500, auto_id=False)
    try:
        poll_for(m, 0.5)
        m.identify()
        ev = poll_for(m, 45, until=lambda ev: any(e.kind == "identified" for e in ev))
        stages = {e.data["stage"] for e in ev if e.kind == "identifying"}
        found = next(e.data for e in ev if e.kind == "identified")
        assert stages >= {"listening", "trying"} and not found["auto"]
        assert found["verified"] and found["verified"][0].name == "POCSAG1200 pager (decoded)", found["tried"]
        assert found["guesses"][0] is found["verified"][0]
    finally:
        m.stop()


def test_channelizer_gives_each_channel_cleanly():
    """Two narrow FM signals in one 2.4 MS/s stream, cut out together: each one's tone comes out
    clean at 48 kHz, with no click where the blocks meet, in odd-sized input blocks."""
    n = RATE * 2
    tt = np.arange(n) / RATE
    x = noise(n, 0.01)
    for off, tone in ((312345.0, 1000.0), (-498765.0, 1500.0)):
        x = x + 0.3 * np.exp(1j * (2 * np.pi * off * tt + 2 * np.pi * 2500 * np.cumsum(np.sin(2 * np.pi * tone * tt)) / RATE))
    ch = dsp.Channelizer(RATE)
    ch.set("a", 312345.0)
    ch.set("b", -498765.0)
    got = {"a": [], "b": []}
    rng = np.random.default_rng(0)
    i = 0
    while i < n:
        k = int(rng.integers(5000, 200000))
        for key, y in ch.process(x[i:i + k].astype(np.complex64)).items():
            got[key].append(dsp.fm_discriminator(ch.channels[key], y, 2500))
        i += k
    for key, tone in (("a", 1000.0), ("b", 1500.0)):
        a = np.concatenate(got[key])[4800:]           # after the filters fill
        assert abs(len(a) - (n / RATE * 48000 - 4800)) < 1200
        assert abs(tone_freq(a) - tone) < 3, key
        ideal = np.sin(2 * np.pi * tone * np.arange(len(a)) / 48000)
        fit = np.linalg.lstsq(np.c_[ideal, np.cos(2 * np.pi * tone * np.arange(len(a)) / 48000)], a, rcond=None)
        resid = a - np.c_[ideal, np.cos(2 * np.pi * tone * np.arange(len(a)) / 48000)] @ fit[0]
        assert np.max(np.abs(resid)) < 0.15, (key, np.max(np.abs(resid)))       # no clicks


def test_decode_points_decode_several_channels_at_once(monkeypatch, tmp_path):
    import signals
    from scipy.signal import resample_poly
    page = np.concatenate([np.zeros(11025), signals.pocsag(1234567, "HELLO PAGER 42", 1200), np.zeros(22050)])
    page = resample_poly(page / np.max(np.abs(page)), 48000, 22050).astype(np.float32)
    aprs = signals.aprs_packets(48000)
    aprs = (aprs / np.max(np.abs(aprs))).astype(np.float32)
    n = max(len(page), len(aprs))
    page, aprs = (np.concatenate([a, np.zeros(n - len(a), np.float32)]) for a in (page, aprs))
    x = fm_iq(page, 300000, 4500) + fm_iq(aprs, -400000, 3000)
    rec = save_iq(tmp_path / "two.cu8", x)
    m = start_receiver(monkeypatch, input="file", file=str(rec), file_rate=RATE, freq_mhz=101.1, demod="WFM",
                       bandwidth=180000)
    try:
        poll_for(m, 0.3)
        centre = m.center
        m.add_point(centre + 300000, "pager")
        m.add_point(centre - 400000, "aprs")
        m.add_point(centre + 5e6, "aprs")                     # outside the band: kept, but silent
        got = lambda ev: {e.data["id"] for e in ev if e.kind == "point_decoded" and (   # noqa: E731
            e.data["event"].kind == "row" or ">" in (e.data["event"].text or ""))}
        ev = poll_for(m, 30, until=lambda ev: got(ev) >= {"pager", "aprs"})
        assert got(ev) >= {"pager", "aprs"}, [(e.data["id"], e.data["event"].text) for e in ev
                                              if e.kind == "point_decoded"][-6:]
        rows = [e.data for e in ev if e.kind == "point_decoded" and e.data["event"].kind == "row"]
        assert rows[0]["freq"] == centre + 300000 and rows[0]["event"].data["message"] == "HELLO PAGER 42"
        states = {s["freq"]: s for s in m.point_states()}
        assert states[centre + 300000]["in_view"] and not states[centre + 5e6]["in_view"]
        assert states[centre + 300000]["heard"] > 0
        m.remove_point(centre + 300000)
        poll_for(m, 0.5)
        assert len(m.points) == 2
    finally:
        m.stop()
    assert m.points == {}


def test_find_carriers_strong_narrow_yes_broadcast_no():
    n, rate, centre = 4096, 2400000, 100e6
    f = centre + (np.arange(n) - n / 2) * rate / n
    db = np.full(n, -100.0)
    near = lambda f0, half: np.abs(f - f0) < half                         # noqa: E731
    db[near(centre + 300000, 5000)] = -30                                  # strong DMR: 70 dB up...
    db[near(centre + 300000, 30000) & (db < -40)] = -80                    # ...skirts 20 dB up, 60 kHz wide
    db[near(centre - 400000, 100000)] = -50                                # an FM broadcast, 200 kHz flat
    found = dsp.find_carriers(db, rate, centre, max_width=25000, min_snr=12, merge=10000)
    assert len(found) == 1 and abs(found[0] - (centre + 300000)) < 2000, found


def test_many_dmr_points_stop_quickly(monkeypatch, tmp_path):
    """Removing DSD-FME points mustn't hold up the receiver, and Stop ends them all at once: they
    used to take 2 s each, one after another (the window hung, the dongle dropped samples)."""
    from sdr_toolkit.modes.digital_voice import dsd_fme
    if dsd_fme() is None:
        pytest.skip("DSD-FME isn't built")
    rec = save_iq(tmp_path / "noise.cu8", noise(RATE * 2, 0.05))
    m = start_receiver(monkeypatch, input="file", file=str(rec), file_rate=RATE, freq_mhz=101.1)
    try:
        poll_for(m, 0.3)
        freqs = [m.center + k * 25000 for k in range(1, 9)]
        for f in freqs:
            m.add_point(f, "dmr")
        poll_for(m, 1.5)
        procs = [pt["host"].mode._proc for pt in m.points.values()]
        assert len(procs) == 8 and all(p.poll() is None for p in procs)
        for f in freqs[:4]:
            m.remove_point(f)
        ev = poll_for(m, 1.0)
        assert len(m.points) == 4
        frames = sum(e.kind == "spectrum" for e in ev)        # 25 a second while the receiver keeps up
        assert frames >= 10, frames
    finally:
        t = time.time()
        m.stop()
        took = time.time() - t
    assert took < 3, took
    assert all(p.poll() is not None for p in procs) and m.src is None


def test_dmr_points_follow_their_signal(monkeypatch, tmp_path):
    """AFC: a point 2.5 kHz beside a DMR signal moves onto it; one on a signal drifting 1.5 kHz
    during the transmission follows it. Noise elsewhere leaves its point where it is."""
    from sdr_toolkit.modes.digital_voice import dsd_fme
    from scipy.signal import resample_poly
    if dsd_fme() is None:
        pytest.skip("DSD-FME isn't built")
    import signals
    a = resample_poly(signals.dmr_bursts(48000, 10.0), RATE // 48000, 1)
    tt = np.arange(len(a)) / RATE
    fsk = 2 * np.pi * 1944 * np.cumsum(a) / RATE
    drift = 2 * np.pi * np.cumsum(-400000 + 1500 * tt / tt[-1]) / RATE           # -400 kHz, rising 1.5 kHz
    x = (0.5 * np.exp(1j * (2 * np.pi * 302500 * tt + fsk)) + 0.5 * np.exp(1j * (drift + fsk))).astype(np.complex64)
    rec = save_iq(tmp_path / "two.cu8", x + noise(len(x), 0.01))
    m = start_receiver(monkeypatch, input="file", file=str(rec), file_rate=RATE, freq_mhz=101.1)
    try:
        poll_for(m, 0.3)
        c = m.center
        m.add_point(c + 300000, "dmr")                  # 2.5 kHz below its signal
        m.add_point(c - 400000, "dmr")                  # on its signal at first, which then drifts up
        m.add_point(c + 700000, "dmr")                  # only noise
        afc, frames = {}, {}
        t0 = time.time()
        while time.time() < t0 + 9.5:
            ev = poll_for(m, 0.5)
            afc = {round(st["freq"] - c): st["afc"] for st in m.point_states()}
            for e in ev:
                if e.kind == "point_decoded" and e.data["event"].kind == "dsd" and not e.data["event"].data.get("error"):
                    k = (round(e.data["freq"] - c), time.time() - t0 > 5)
                    frames[k] = frames.get(k, 0) + 1
        assert abs(afc[300000] - 2500) < 400, afc
        assert frames.get((300000, True), 0) > 0, frames    # decoding once it's on the signal
        assert 900 < afc[-400000] < 1900, afc            # about 1.4 kHz up by now
        assert afc[700000] == 0, afc
    finally:
        m.stop()


def test_explorer_lists_and_listening(tmp_path):
    from sdr_toolkit.receiver import dsd
    from sdr_toolkit.receiver.explore import Explorer
    ex = Explorer()
    t = 1000.0
    for line in ("Sync: +DMR  [SLOT1]  slot2  | Color Code=01 | VLC", " SLOT 1 TGT=505 SRC=3125678 Group Call",
                 "Sync: +DMR  [SLOT1]  slot2  | Color Code=01 | VC1", " Lat: -27.6 Lon: 153.1 (-27.6, 153.1)"):
        ex.add(438.8e6, dsd.parse(line), t)
    for line in ["Sync: +P25p1   NAC: 654; TSBK"] * 4 + ["  WACN [BEE00] SYSID [658] NAC [654]"]:
        ex.add(419.9875e6, dsd.parse(line), t)
    ex.add(473.1e6, dsd.parse(" SLOT 2 TGT=9 SRC=42 Private Encrypted Call"), t)
    assert ex.talkgroups[505]["calls"] == 1 and ex.talkgroups[505]["radios"] == {3125678}
    assert ex.radios[3125678]["position"] == (-27.6, 153.1) and ex.radios[42]["last_tg"] == 9
    kinds = {s["id"]: s for s in ex.systems()}
    assert kinds["WACN BEE00 SYSID 658"]["control"] == [419.9875e6] and kinds["colour code 1"]["voice"] == [438.8e6]
    assert any("Group call to 505 from radio 3125678" in x[2] for x in ex.log)
    # Listening: everything; one channel; one talkgroup (only while its call is on).
    assert ex.audible(438.8e6, t) and ex.audible(473.1e6, t)
    ex.only = ("freq", 473.1e6)
    assert ex.audible(473.1e6, t) and not ex.audible(438.8e6, t)
    ex.only = ("tg", 505)
    assert ex.audible(438.8e6, t + 1) and not ex.audible(473.1e6, t + 1) and not ex.audible(438.8e6, t + 10)
    ex.only = ("radio", 42)
    assert ex.audible(473.1e6, t + 1) and not ex.audible(438.8e6, t + 1)
    # Kept between sessions.
    ex.save(tmp_path / "explorer.json")
    back = Explorer.load(tmp_path / "explorer.json")
    assert back.channels[419.9875e6].what() == "P25 phase 1 control channel" and back.talkgroups[505]["radios"] == {3125678}
    assert back.radios[3125678]["position"] == (-27.6, 153.1) and len(back.log) == len(ex.log)
    assert Explorer.load(tmp_path / "missing.json").channels == {}


def test_explorer_finds_signals_and_lets_go_of_non_digital(monkeypatch, tmp_path):
    from sdr_toolkit.modes import receiver as R
    from sdr_toolkit.modes.digital_voice import dsd_fme
    if dsd_fme() is None:
        pytest.skip("DSD-FME isn't built")
    import signals
    monkeypatch.setattr(R, "PROBE_FOR", 6.0)
    dmr = fm_iq(signals.dmr_bursts(48000, 4.0), 303400, 1944)     # 3.4 kHz off the 6.25 kHz raster
    tt = np.arange(len(dmr)) / RATE
    x = dmr + (0.2 * np.exp(2j * np.pi * -400000 * tt)).astype(np.complex64)     # a plain carrier: not digital
    rec = save_iq(tmp_path / "band.cu8", x)
    monkeypatch.setattr(R, "AudioOut", FakeAudio)
    from sdr_toolkit.modes.explorer import ExplorerMode
    m = ExplorerMode()
    m.start(dict(m.values, input="file", file=str(rec), file_rate=RATE, freq_mhz=101.1))
    try:
        centre = None
        states = {}
        end = time.time() + 30
        while time.time() < end:
            time.sleep(0.1)
            for e in m.poll():
                centre = centre or m.center
                if e.kind == "point_decoded" and e.data["event"].kind == "explorer":
                    states.setdefault(e.data["freq"], []).append(e.data["event"].data["state"])
                if e.kind == "point_decoded" and e.data["event"].kind == "dsd":
                    states.setdefault(e.data["freq"], []).append("dsd")
            if any("not digital" in v for v in states.values()) and any("dsd" in v for v in states.values()):
                break
        # On the signals themselves (to 100 Hz), not on a 6.25 kHz raster.
        dmr_f = next((f for f in states if abs(f - (centre + 303400)) < 1000), None)
        cw_f = next((f for f in states if abs(f - (centre - 400000)) < 1500), None)
        assert "probing" in states.get(dmr_f, []) and "dsd" in states.get(dmr_f, []), states.keys()
        assert states.get(cw_f, [])[:1] == ["probing"] and "not digital" in states[cw_f], states.keys()
        end = time.time() + 3
        while cw_f in m.points and time.time() < end:                 # its decode point is removed
            time.sleep(0.1)
        assert dmr_f in m.points and cw_f not in m.points
        assert set(states) == {dmr_f, cw_f}, states.keys()             # its side bands aren't signals
    finally:
        m.stop()


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


def test_survey_dwell_catches_a_burst():
    """A burst 50 ms into the slice: missed with no dwell, caught with 100 ms, and the slice takes that long."""
    rng = np.random.default_rng(3)
    t = np.arange(int(0.2 * RATE)) / RATE
    x = (rng.normal(0, 0.01, len(t)) + 1j * rng.normal(0, 0.01, len(t))).astype(np.complex64)
    burst = (t > 0.05) & (t < 0.06)
    x[burst] += 0.3 * np.exp(2j * np.pi * 400000 * t[burst])
    for dwell, caught in ((0.0, False), (0.1, True)):
        sv = SV.Survey(RATE, tuner=5, settle=0.0, dwell=dwell)
        sv.set_view(100e6, 101e6)
        sv.tuned(sv.next_center(), 0.0)
        used = 0
        for i in range(0, len(x), 16384):
            used = i + 16384
            if sv.feed(x[i:i + 16384], 1.0):
                break
        d = np.nan_to_num(sv.stitched(1000), nan=-200)
        assert (np.max(d) > -30) == caught, (dwell, np.max(d))
        if dwell:
            assert 0.1 <= used / RATE < 0.12


def test_capture_keeps_full_detail_and_history():
    res = RATE / 1024
    cap = SV.Capture(24e6, 1766e6, res, max_rows=5)
    db = np.full(820, -90, np.float32)
    db[400], db[404] = -20, -25                       # two signals 4 bins (about 9 kHz) apart
    f_lo = 100e6
    cap.add(f_lo, db)
    cap.push_row()
    f1, f2 = f_lo + 400 * res, f_lo + 404 * res
    wide = cap.spectrum(24e6, 1766e6, 1000)           # 1.7 MHz a column: one blip
    assert np.nanmax(wide) == -20 and np.sum(np.nan_to_num(wide, nan=-200) > -50) == 1
    close = cap.spectrum(f1 - 20e3, f1 + 30e3, 500)  # zoomed in: both, apart, at their frequencies
    xs = np.linspace(f1 - 20e3, f1 + 30e3, 500, endpoint=False)
    loud = xs[close > -50]
    assert abs(loud.min() - f1) < res and abs(loud.max() - f2) < 2 * res and np.any(close[(xs > f1 + res) & (xs < f2 - res)] < -80)
    # The waterfall of any range is rebuilt from the rows: zooming in after the fact shows it.
    wf = cap.waterfall(f1 - 20e3, f1 + 30e3, 500)
    assert wf.count == 1 and np.nanmax(wf.image[0]) == -20
    # A newer measurement replaces the old one (no stale peaks); rows past the limit fold into the base.
    db2 = np.full(820, -90, np.float32)
    for _ in range(7):
        cap.add(f_lo, db2)
        cap.push_row()
    assert len(cap.rows) == 5 and np.nanmax(cap.spectrum(f1 - 20e3, f1 + 30e3, 500)) == -90
    wf = cap.waterfall(f1 - 20e3, f1 + 30e3, 500)
    assert wf.count == 5 and np.nanmax(wf.image[:5]) == -90
    wf.add_row([(cap.index(f1), np.array([-10], np.float16))])
    assert wf.count == 5 and np.nanmax(wf.image[0]) == -10 and np.nanmax(wf.image[1]) == -90


def test_receiver_surveys_with_retune_commands(monkeypatch, tmp_path):
    from sdr_toolkit.receiver.source import SET_FREQ
    rec = fm_station(tmp_path / "fm.cu8", seconds=0.5)
    m = start_receiver(monkeypatch, input="file", file=str(rec), file_rate=RATE, settle_ms=20)
    try:
        m.start_survey(90e6, 110e6)
        ev = poll_for(m, 3.0)
        frames = [e.data for e in ev if e.kind == "survey"]
        assert sum(len(f["slices"]) for f in frames) > 3 and frames[-1]["res"] == RATE / 1024
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
