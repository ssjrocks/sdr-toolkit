"""Every mode, fed a test signal through the same interface the window uses.

    python3 -m pytest tests
"""

import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import signals  # noqa: E402
from sdr_toolkit.engines.morse.synth import transmission as morse_tx  # noqa: E402
from sdr_toolkit.engines.navtex import encode as navtex_words  # noqa: E402
from sdr_toolkit.engines.navtex import to_audio as navtex_audio  # noqa: E402
from sdr_toolkit.engines.psk import encode_bits, to_audio as psk_audio  # noqa: E402
from sdr_toolkit.engines.sstv.modes import BY_NAME  # noqa: E402
from sdr_toolkit.engines.sstv.synth import color_bars, encode as sstv_encode  # noqa: E402
from sdr_toolkit.engines.wefax import to_audio as wefax_audio  # noqa: E402
from sdr_toolkit.modes import MODES  # noqa: E402

BY_ID = {M.id: M for M in MODES}


def run(mode_id, audio, chunk=0.05, start_time=1_800_000_000.0, **values):
    mode = BY_ID[mode_id]()
    v = dict(mode.values)
    v.update(values)
    mode.start(v)
    events = []
    n = max(int(chunk * mode.rate), 1)
    for i in range(0, len(audio), n):
        events += mode.feed(audio[i : i + n], start_time + (i + n) / mode.rate)
        events += mode.poll()
    events += mode.stop()
    return mode, events


def lines(events):
    return [e.text for e in events if e.kind == "line"]


def text(events):
    return "\n".join(lines(events))


def pad(a, rate, before=0.5, after=1.5, level=0.0):
    out = np.concatenate([np.zeros(int(before * rate), np.float32), a, np.zeros(int(after * rate), np.float32)])
    return out + signals.noise(len(out), level) if level else out


def test_every_mode_has_tuning_help_and_settings():
    for M in MODES:
        m = M()
        assert m.tuning.sdr_mode and m.tuning.bandwidth and m.summary, m.id
        for s in m.settings:
            assert s.key in m.values
            if s.kind == "choice":
                assert any(v == s.default for v, _ in s.choices), (m.id, s.key)


@pytest.mark.parametrize("tone", [600, 700, 850])
def test_morse_at_any_tone(tone):
    a = morse_tx("CQ TEST DE VK2ABC", 25, 20)                     # made at 700 Hz
    a = signals.resample(a, 8000, 8000)
    if tone != 700:
        # Move the whole signal: mix with a carrier and keep the upper sideband.
        from scipy.signal import hilbert
        t = np.arange(len(a)) / 8000
        a = np.real(hilbert(a) * np.exp(2j * np.pi * (tone - 700) * t)).astype(np.float32)
    _, ev = run("morse", pad(a, 8000, after=3), tone=tone, speed="any")
    assert "CQ TEST DE VK2ABC" in text(ev)


def test_morse_misses_a_signal_off_its_tone():
    a = morse_tx("PARIS", 30, 20)
    _, ev = run("morse", pad(a, 8000, after=3), tone=1200)
    assert "PARIS" not in text(ev)


@pytest.mark.parametrize("mark,shift,baud", [(2125, 170, 45.45), (1500, 450, 50.0), (2000, 850, 75.0)])
def test_rtty(mark, shift, baud):
    a = signals.rtty("RYRY CQ DE VK2ABC 599 TEST 73", 8000, mark, shift, baud)
    _, ev = run("rtty", pad(a, 8000, level=0.05), mark=mark, shift=shift, baud=baud)
    out = text(ev) + "".join(e.text for e in ev if e.kind == "live")[-200:]
    assert "CQ DE VK2ABC 599 TEST 73" in out


def test_rtty_reverse():
    a = signals.rtty("THE QUICK BROWN FOX", 8000, 2295, -170)        # space below mark
    _, ev = run("rtty", pad(a, 8000), mark=2295, reverse=True)
    assert "THE QUICK BROWN FOX" in "".join(e.text for e in ev if e.kind in ("line", "live"))


@pytest.mark.parametrize("baud", [31.25, 62.5, 125.0])
def test_psk(baud):
    msg = "CQ CQ de VK2ABC pse k"
    a = psk_audio(encode_bits(msg + "\n"), 8000, 1503, baud)
    _, ev = run("psk", pad(a, 8000, level=0.05), freq=1500, baud=baud)
    assert msg in text(ev)


def test_psk_click_to_tune():
    m = BY_ID["psk"]()
    m.start(dict(m.values))
    assert m.set_frequency(1234.4) == "freq" and m.values["freq"] == 1234 and m.dec.freq == 1234


def test_navtex_filters_stations():
    words = navtex_words("ZCZC EA01\r\nGALE WARNING\r\nNNNN\r\n") + \
        navtex_words("ZCZC KB02\r\nOTHER\r\nNNNN\r\n", phasing_s=0.5)
    a = pad(navtex_audio(words, 8000), 8000, level=0.1)
    _, ev = run("navtex", a)
    assert "GALE WARNING" in text(ev) and "OTHER" in text(ev)
    _, ev = run("navtex", a, stations="E")
    assert "GALE WARNING" in text(ev) and "OTHER" not in text(ev)


def test_navtex_reversed_bit_order_and_polarity():
    words = navtex_words("ZCZC EA01\r\nGALE WARNING\r\nNNNN\r\n")
    a = pad(navtex_audio(words, 8000, lsb_first=True, invert=True), 8000)
    _, ev = run("navtex", a)
    assert "GALE WARNING" in text(ev)


def test_sstv(tmp_path):
    mode = BY_NAME["Robot 36"]
    a = pad(sstv_encode(color_bars(320, 240), mode), 12000)
    _, ev = run("sstv", a, folder=str(tmp_path))
    done = [e for e in ev if e.kind == "image" and e.done]
    assert len(done) == 1 and "complete" in done[0].title
    assert len(list(tmp_path.glob("*_Robot36.png"))) == 1


def test_wefax(tmp_path):
    img = np.tile(np.linspace(0, 255, 1810).astype(np.uint8), (60, 1))
    a = pad(wefax_audio(img, 12000), 12000, after=3)
    m, ev = run("wefax", a, folder=str(tmp_path))
    done = [e for e in ev if e.kind == "image" and e.done]
    assert len(done) == 1
    rows = done[0].image[:, :, 0].astype(int)
    assert 55 <= len(rows) <= 62
    assert np.abs(rows[5:50] - img[0]).mean() < 8
    assert len(list(tmp_path.glob("*_WEFAX.png"))) == 1


def test_tones():
    d = signals.dcs(0o023, 4)
    with_dcs = d + signals.ctcss_voice(0, 5)[: len(d)]      # voice without a CTCSS tone
    a = np.concatenate([signals.dtmf("123#"), np.zeros(8000 * 3, np.float32),
                        signals.ctcss_voice(88.5, 4), np.zeros(8000 * 2, np.float32), with_dcs])
    _, ev = run("tones", a)
    out = text(ev)
    assert "DTMF  123#" in out
    assert "CTCSS 88.5 Hz" in out
    assert "DCS D023N" in out


def test_aprs():
    a = signals.aprs_packets(48000)
    _, ev = run("aprs", pad(a, 48000))
    pk = [t for t in lines(ev) if "WB2OSZ-15>TEST" in t]
    assert len(pk) == 4


@pytest.mark.parametrize("baud", [512, 1200, 2400])
def test_pager(baud):
    a = signals.resample(signals.pocsag(1234567, "HELLO PAGER 42", baud), 22050, 22050)
    _, ev = run("pager", pad(a, 22050))
    rows = [e.data for e in ev if e.kind == "row"]
    assert any(r["address"] == "1234567" and r["message"] == "HELLO PAGER 42" and
               r["proto"] == f"POCSAG{baud}" for r in rows), rows


def test_pager_address_filter_and_invert():
    a = pad(signals.pocsag(1234567, "INVERTED", 1200, invert=True), 22050)
    _, ev = run("pager", a)
    assert not [e for e in ev if e.kind == "row" and e.data["message"] == "INVERTED"]
    _, ev = run("pager", a, invert=True)
    assert [e for e in ev if e.kind == "row" and e.data["message"] == "INVERTED"]
    _, ev = run("pager", a, invert=True, only="7654321")
    assert not [e for e in ev if e.kind == "row"]


def _wsjt(mode_id, slot_audio, period, **values):
    """Feed one slot (plus a little either side) on the wall clock it belongs to."""
    start = 1_800_000_000.0 - 1_800_000_000.0 % period + period     # a slot boundary
    a = np.concatenate([np.zeros(12000, np.float32), slot_audio, np.zeros(int(period * 12000), np.float32)])
    mode = BY_ID[mode_id]()
    v = dict(mode.values)
    v.update(values)
    mode.start(v)
    events = []
    n = 600
    for i in range(0, len(a), n):
        events += mode.feed(a[i : i + n], start - 1.0 + (i + n) / 12000)
        events += mode.poll()
    deadline = time.time() + 30
    while time.time() < deadline and mode._busy:
        time.sleep(0.2)
        events += mode.poll()
    events += mode.stop()
    return mode, events


def test_ft8():
    slot = signals.ft8("CQ VK2ABC QF56", 1200, -10)
    mode, ev = _wsjt("ft8", slot, 15.0, auto_delay=False)
    rows = [e.data for e in ev if e.kind == "row"]
    assert [r["message"] for r in rows] == ["CQ VK2ABC QF56"]
    assert abs(rows[0]["freq"] - 1200) < 5 and abs(rows[0]["dt"]) < 0.5


def test_ft8_audio_delay_followed():
    # The audio arrives 1.5 s late (a WebSDR stream); Follow DT moves the delay.
    slot = np.concatenate([np.zeros(18000, np.float32), signals.ft8("CQ VK2ABC QF56", 1500, -5)])[:180000]
    mode, ev = _wsjt("ft8", slot, 15.0, auto_delay=True)
    rows = [e.data for e in ev if e.kind == "row"]
    assert rows and abs(rows[0]["dt"] - 1.5) < 0.3
    assert 0.8 < mode.values["delay"] < 1.6
    assert [e for e in ev if e.kind == "setting"]


def test_ft4():
    slot = signals.ft4("CQ VK2ABC QF56", 1000)
    _, ev = _wsjt("ft4", slot, 7.5, auto_delay=False)
    assert [e.data["message"] for e in ev if e.kind == "row"] == ["CQ VK2ABC QF56"]


@pytest.mark.parametrize("mode_id", ["morse", "rtty", "psk", "navtex", "tones"])
def test_quiet_on_noise(mode_id):
    # Noise (a receiver with nothing on frequency) must not print text.
    rate = BY_ID[mode_id].rate
    _, ev = run(mode_id, signals.noise(rate * 20, 0.1, seed=3))
    printed = "".join(e.text for e in ev if e.kind in ("line", "live")).strip()
    assert len(printed) <= 2, printed


# ---- RTL-SDR modes, from IQ recordings

import radio_signals  # noqa: E402


def run_radio(mode_id, path, wait=30.0, **values):
    mode = BY_ID[mode_id]()
    v = dict(mode.values)
    v.update(values, input="file", file=str(path))
    mode.start(v)
    events = []
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.2)
        events += mode.poll()
        if mode._proc.poll() is not None and mode._lines.empty():
            break
    time.sleep(0.3)
    events += mode.poll()
    events += mode.stop()
    return mode, events


def test_ism_nexus_sensor(tmp_path):
    path = radio_signals.save(tmp_path / "nexus.cu8", radio_signals.nexus_iq(0xA5, 2, 21.7, 58))
    _, ev = run_radio("ism", path)
    rows = [e.data for e in ev if e.kind == "row"]
    assert rows and rows[0]["model"] == "Nexus-TH" and rows[0]["id"] == 165 and rows[0]["channel"] == 2
    assert "temperature_C=21.7" in rows[0]["data"] and "humidity=58" in rows[0]["data"]


def test_ism_protocol_filter(tmp_path):
    path = radio_signals.save(tmp_path / "nexus.cu8", radio_signals.nexus_iq())
    _, ev = run_radio("ism", path, protocols="73")          # LaCrosse only: the Nexus is ignored
    assert not [e for e in ev if e.kind == "row"]


def test_adsb(tmp_path):
    path = radio_signals.save(tmp_path / "adsb.cu8", radio_signals.adsb_iq(0x7C1234, "QFA123", -33.95, 151.18, 12000))
    _, ev = run_radio("adsb", path, lat="-33.87", lon="151.21")
    tables = [e.data["rows"] for e in ev if e.kind == "table" and e.data["rows"]]
    assert tables, "no aircraft"
    a = tables[-1][0]
    assert a["hex"] == "7C1234" and a["flight"] == "QFA123" and a["alt"] == 12000
    lat, lon = (float(x) for x in a["pos"].split(","))
    assert abs(lat + 33.95) < 0.001 and abs(lon - 151.18) < 0.001
    assert a["dist"].split()[1] == "km" and 8 < float(a["dist"].split()[0]) < 12      # 9.3 km


def test_ais(tmp_path):
    path = radio_signals.save(tmp_path / "ais.cu8", radio_signals.ais_iq(repeats=5))
    _, ev = run_radio("ais", path, file_rate=240000)
    out = text(ev)
    # Bursts start abruptly out of noise; direwolf misses about 1 in 20 of these at any level.
    assert out.count("Position Report Class A") >= 4, out
    assert '"503123456"' in out and "S 33 51.0000, E 151 12.6000" in out


def test_ais_channel_offset(tmp_path):
    # The AIS channel 50 kHz off the recording's centre.
    path = radio_signals.save(tmp_path / "ais.cu8", radio_signals.ais_iq(repeats=2, offset_hz=50000))
    _, ev = run_radio("ais", path, file_rate=240000, file_offset=50000)
    assert text(ev).count("Position Report Class A") >= 1


def _dongle_present():
    import subprocess
    out = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    return any(i in out for i in ("0bda:2838", "0bda:2832"))


@pytest.mark.skipif(_dongle_present(), reason="an RTL-SDR is plugged in; this test must not touch it")
@pytest.mark.parametrize("mode_id", ["ism", "adsb", "ais"])
def test_live_without_dongle_says_so(mode_id):
    mode = BY_ID[mode_id]()
    mode.start(dict(mode.values))            # input: RTL-SDR
    events = []
    deadline = time.time() + 10
    while time.time() < deadline and not any(e.kind == "line" and "No RTL-SDR found" in e.text for e in events):
        time.sleep(0.2)
        events += mode.poll()
    events += mode.stop()
    assert any("No RTL-SDR found" in e.text for e in events if e.kind == "line"), [e.text for e in events]


# ---- LoRa: Meshtastic and MeshCore packets

from sdr_toolkit.engines import mesh  # noqa: E402


def test_meshtastic_default_channel():
    key = mesh.meshtastic_key("AQ==")
    assert key == mesh.DEFAULT_PSK
    # Meshtastic's LongFast channel carries hash 8 (name XOR key bytes).
    assert mesh.channel_hash("LongFast", key) == 0x08
    text = mesh.pb_encode([(1, 1), (2, "Hello mesh")])
    frame = mesh.build_meshtastic(0x1234ABCD, 0x5555, text, key, 0x08)
    p = mesh.parse_meshtastic(frame, [("LongFast", key)])
    assert p.sender == 0x1234ABCD and p.dest == 0xFFFFFFFF and p.decoded["text"] == "Hello mesh"
    pos = mesh.pb_encode([(1, ("f32", -338700000)), (2, ("f32", 1512100000)), (3, 42)])
    p = mesh.parse_meshtastic(mesh.build_meshtastic(1, 2, mesh.pb_encode([(1, 3), (2, pos)]), key, 8),
                              [("LongFast", key)])
    assert abs(p.decoded["lat"] + 33.87) < 1e-6 and abs(p.decoded["lon"] - 151.21) < 1e-6 and p.decoded["alt"] == 42
    user = mesh.pb_encode([(1, "!00000001"), (2, "Hill Node"), (3, "HN")])
    p = mesh.parse_meshtastic(mesh.build_meshtastic(1, 3, mesh.pb_encode([(1, 4), (2, user)]), key, 8),
                              [("LongFast", key)])
    assert p.decoded["long_name"] == "Hill Node" and p.decoded["short_name"] == "HN"


def test_meshtastic_private_channel_stays_encrypted_without_key():
    secret = bytes(range(16))
    frame = mesh.build_meshtastic(1, 9, mesh.pb_encode([(1, 1), (2, "private")]), secret, 0x55)
    p = mesh.parse_meshtastic(frame, [("LongFast", mesh.DEFAULT_PSK)])
    assert not p.decoded and "encrypted" in mesh.describe_meshtastic(p)
    p = mesh.parse_meshtastic(frame, [("LongFast", mesh.DEFAULT_PSK), ("Club", secret)])
    assert p.decoded["text"] == "private" and p.decoded["channel"] == "Club"


def test_meshtastic_slot_frequencies():
    from sdr_toolkit.modes.lora import meshtastic_freq
    # The documented defaults (US slot 20, EU 868, EU 433), and the ANZ one they imply.
    assert round(meshtastic_freq("US", "LongFast", 250000), 3) == 906.875
    assert round(meshtastic_freq("EU_868", "LongFast", 250000), 3) == 869.525
    assert round(meshtastic_freq("EU_433", "LongFast", 250000), 3) == 433.875
    assert round(meshtastic_freq("ANZ", "LongFast", 250000), 3) == 919.875


def test_meshcore_public_channel_and_advert():
    key = mesh.meshcore_key("izOH6cXN6mrJ5e26oRXNcg==")
    assert key == mesh.PUBLIC_KEY == mesh.meshcore_key("8b3387e9c5cdea6ac9e5edbaa115cd72")
    frame = mesh.build_meshcore(5, mesh.build_group_text(key, 1794200000, "Alice", "hi all"), path=b"\x3a")
    p = mesh.parse_meshcore(frame, [("Public", key)])
    assert p.kind == "group text" and p.decoded["text"] == "Alice: hi all" and p.path == b"\x3a"
    # A wrong key fails the MAC instead of printing garbage.
    p = mesh.parse_meshcore(frame, [("Other", bytes(16))])
    assert not p.decoded
    adv = mesh.build_advert(bytes(range(32)), 1794200000, "Hilltop RPT", 2, -33.8, 151.2)
    p = mesh.parse_meshcore(mesh.build_meshcore(4, adv), [])
    assert p.decoded["name"] == "Hilltop RPT" and p.decoded["node_kind"] == "repeater"
    assert abs(p.decoded["lat"] + 33.8) < 1e-6


def _lora_mode(**values):
    m = BY_ID["lora"]()
    m.values.update(values)
    m._mt_keys, m._mc_keys = m._keys()
    m._frame, m._summary = None, ""
    return m


def _receiver_lines(frame, crc_ok=True):
    return ["rx msg: " + ", ".join(f"0x{b:x}" for b in frame), "CRC valid!" if crc_ok else "\x1b[31mCRC invalid\x1b[0m"]


def test_lora_mode_reads_receiver_output():
    key = mesh.DEFAULT_PSK
    frame = mesh.build_meshtastic(0xA1B2C3D4, 77, mesh.pb_encode([(1, 1), (2, "CQ mesh")]), key, 8)
    m = _lora_mode()
    ev = sum((m.parse(line) for line in _receiver_lines(frame)), [])
    assert any('"CQ mesh"' in e.text and "!a1b2c3d4" in e.text for e in ev if e.kind == "line")
    ev = sum((m.parse(line) for line in _receiver_lines(frame, crc_ok=False)), [])
    assert not [e for e in ev if e.kind == "line"]          # bad CRC hidden by default
    m = _lora_mode(preset="meshcore")
    mc = mesh.build_meshcore(5, mesh.build_group_text(mesh.PUBLIC_KEY, 1, "Bob", "evening"))
    ev = sum((m.parse(line) for line in _receiver_lines(mc)), [])
    assert any("[Public] Bob: evening" in e.text for e in ev if e.kind == "line")


def test_lora_without_gr_lora_sdr_asks_for_it(monkeypatch):
    import sdr_toolkit.modes.lora as lora
    from sdr_toolkit.modes.base import MissingParts
    monkeypatch.setattr(lora, "lora_available", lambda: None)
    m = BY_ID["lora"]()
    with pytest.raises(MissingParts) as e:
        m.start(dict(m.values))
    assert e.value.parts == ["gr-lora_sdr"]


def test_every_requirement_is_a_known_part():
    from sdr_toolkit import deps
    for M in MODES:
        for part in M.requires:
            assert part in deps.PARTS, (M.id, part)


def test_install_runs_setup_steps_in_order(tmp_path, monkeypatch):
    """deps.install: system.sh through pkexec, then build.sh with the mbelib answer; output logged."""
    from sdr_toolkit import deps
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    (bin_dir / "pkexec").write_text(f'#!/bin/sh\necho "pkexec $*" >> {calls}\nexec "$@"\n')
    (bin_dir / "pkexec").chmod(0o755)
    setup = tmp_path / "setup"
    setup.mkdir()
    (setup / "system.sh").write_text(f'echo "system.sh" >> {calls}\necho "STEP packages done"\n')
    (setup / "build.sh").write_text(f'echo "build.sh $*" >> {calls}\necho "STEP builds done"\n')
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setattr(deps, "setup_script", lambda name: str(setup / name))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state = {"readsb": False, "dsd-fme": False}
    monkeypatch.setitem(deps.PARTS, "readsb", ("readsb", lambda: state["readsb"], "system"))
    monkeypatch.setitem(deps.PARTS, "dsd-fme", ("dsd", lambda: state["dsd-fme"], "mbelib"))
    lines = []
    still = deps.install(["readsb", "dsd-fme"], True, lines.append)
    assert calls.read_text().splitlines() == [f"pkexec bash {setup}/system.sh", "system.sh",
                                              "build.sh --accept-mbelib"]
    assert "STEP packages done" in lines and "STEP builds done" in lines
    assert still == ["readsb", "dsd-fme"]          # the fakes installed nothing
    assert "STEP builds done" in deps.log_file().read_text()
    # A cancelled password prompt (pkexec exits 126) stops before building.
    (bin_dir / "pkexec").write_text("#!/bin/sh\nexit 126\n")
    calls.write_text("")
    lines = []
    deps.install(["readsb", "dsd-fme"], False, lines.append)
    assert calls.read_text() == "" and any("cancelled" in x for x in lines)


from sdr_toolkit.modes.lora import lora_available  # noqa: E402
from sdr_toolkit.modes.radio import write_cu8  # noqa: E402

needs_lora = pytest.mark.skipif(lora_available() is None, reason="gr-lora_sdr isn't built (scripts/build-gr-lora-sdr.sh)")


def lora_recording(path, frames, rate, bw, sf, cr, sync, preamble, centre_hz, ppm=5.0):
    """LoRa IQ from gr-lora_sdr's transmitter, with a crystal error and noise, as a .cu8 file."""
    import subprocess
    env = lora_available()
    c64 = str(path) + ".c64"
    subprocess.run([sys.executable, str(Path(__file__).parent / "lora_tx.py"), c64, "--rate", str(rate),
                    "--bw", str(bw), "--sf", str(sf), "--cr", str(cr), "--sync", sync, "--preamble",
                    str(preamble)] + [f.hex() for f in frames], env=env, check=True, timeout=120)
    x = np.fromfile(c64, dtype=np.complex64)
    x = np.concatenate([np.zeros(rate // 5, np.complex64), x, np.zeros(rate // 5, np.complex64)])
    e = ppm * 1e-6
    n = np.arange(int(len(x) / (1 + e)))
    t = n * (1 + e)
    xr = np.interp(t, np.arange(len(x)), x.real) + 1j * np.interp(t, np.arange(len(x)), x.imag)
    rng = np.random.default_rng(0)
    y = 0.5 * xr * np.exp(2j * np.pi * e * centre_hz * n / rate) + (rng.standard_normal(len(n)) + 1j * rng.standard_normal(len(n))) * 0.05
    write_cu8(path, y)
    return path


@needs_lora
def test_lora_meshtastic_longfast(tmp_path):
    key = mesh.DEFAULT_PSK
    frames = [mesh.build_meshtastic(0xA1B2C3D4, 1001, mesh.pb_encode([(1, 1), (2, "Hello from the toolkit")]), key, 8),
              mesh.build_meshtastic(0xA1B2C3D4, 1002, mesh.pb_encode(
                  [(1, 3), (2, mesh.pb_encode([(1, ("f32", -338700000)), (2, ("f32", 1512100000))]))]), key, 8)]
    path = lora_recording(tmp_path / "mt.cu8", frames, 1000000, 250000, 11, 1, "0x2B", 16, 919.875e6)
    _, ev = run_radio("lora", path, wait=120, preset="mt:LongFast", region="ANZ", file_rate=1000000)
    out = text(ev)
    assert '!a1b2c3d4 → everyone [LongFast]: "Hello from the toolkit"' in out, out
    assert "position -33.87000, 151.21000" in out, out


@needs_lora
def test_lora_meshcore_anz(tmp_path):
    frames = [mesh.build_meshcore(5, mesh.build_group_text(mesh.PUBLIC_KEY, 1794200000, "Alice", "hi from the hill")),
              mesh.build_meshcore(4, mesh.build_advert(bytes(range(32)), 1794200000, "Hilltop RPT", 2, -33.8, 151.2))]
    path = lora_recording(tmp_path / "mc.cu8", frames, 250000, 62500, 7, 3, "0x12", 8, 916.575e6)
    _, ev = run_radio("lora", path, wait=120, preset="meshcore", region="ANZ", file_rate=250000)
    out = text(ev)
    assert "[Public] Alice: hi from the hill" in out, out
    assert "advert: Hilltop RPT (repeater) at -33.80000, 151.20000" in out, out


# ---- DMR / P25 (DSD-FME). No test signal generator exists for these protocols here, so the
# tests cover the plumbing; decoding real signals needs a dongle.

from sdr_toolkit.modes.digital_voice import dsd_fme  # noqa: E402

needs_dsd = pytest.mark.skipif(dsd_fme() is None, reason="DSD-FME isn't built (scripts/build-dsd-fme.sh)")


@needs_dsd
def test_dmr_recording_of_noise_shows_no_calls(tmp_path):
    rng = np.random.default_rng(4)
    n = 240000 * 4
    path = tmp_path / "noise.cu8"
    write_cu8(path, (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.2)
    mode, ev = run_radio("dmr", path, wait=60, voice=False, file_rate=240000)
    assert not [e for e in ev if e.kind == "line" and e.style != "dim"], [e.text for e in ev]
    assert any(e.kind == "status" and "recording finished" in e.text for e in ev)


@needs_dsd
def test_dmr_command_line():
    m = BY_ID["dmr"]()
    m.values.update(freq_mhz="438.875", protocol="-f1", bw=12, squelch=5, voice=False, ppm=-3, gain="30")
    cmd = m.command()
    assert cmd[1:4] == ["-f1", "-i", "rtl:0:438.875M:30:-3:12:5:2"] and "-o" in cmd and cmd[cmd.index("-o") + 1] == "null"
    assert "-H" not in cmd                       # no decryption keys, ever
    shutil.rmtree(m._dir)


@needs_dsd
def test_dmr_needs_a_frequency():
    m = BY_ID["dmr"]()
    with pytest.raises(RuntimeError, match="frequency"):
        m.start(dict(m.values))


@needs_dsd
@pytest.mark.skipif(_dongle_present(), reason="an RTL-SDR is plugged in; this test must not touch it")
def test_dmr_live_without_dongle_says_so():
    m = BY_ID["dmr"]()
    v = dict(m.values)
    v.update(freq_mhz="438.875", voice=False)
    m.start(v)
    events = []
    deadline = time.time() + 15
    while time.time() < deadline and not any("No RTL-SDR found" in e.text for e in events if e.kind == "line"):
        time.sleep(0.2)
        events += m.poll()
    events += m.stop()
    assert any("No RTL-SDR found" in e.text for e in events if e.kind == "line")


# ---- updates

def test_updater_trusts_only_this_repository():
    from sdr_toolkit import updater
    assert updater._trusted("https://api.github.com/repos/ssjrocks/sdr-toolkit/releases/assets/1")
    assert updater._trusted("https://github.com/ssjrocks/sdr-toolkit/releases/download/v1/x.whl")
    assert not updater._trusted("https://github.com/someone/sdr-toolkit/releases/download/v1/x.whl")
    assert not updater._trusted("https://example.com/ssjrocks/sdr-toolkit/x.whl")
    assert updater.parse_version("v0.10.1") > updater.parse_version("0.9.9")


def test_updater_refuses_a_wheel_with_the_wrong_checksum(monkeypatch, tmp_path):
    from sdr_toolkit import updater
    monkeypatch.setattr(updater, "installed_in_venv", lambda: True)
    monkeypatch.setattr(updater, "_download", lambda *a: b"not the real wheel")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    ran = []
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: ran.append(a))
    u = updater.Update("9.9.9", "https://api.github.com/repos/ssjrocks/sdr-toolkit/releases/assets/1",
                       "sdr_toolkit-9.9.9-py3-none-any.whl", "0" * 64)
    with pytest.raises(RuntimeError, match="checksum"):
        updater.install(u)
    assert not ran                                   # pip never ran


def test_updater_falls_back_to_the_plain_link(monkeypatch):
    from sdr_toolkit import updater
    tried = []

    def get(url, accept=None, timeout=20):
        tried.append(url)
        if "api.github.com" in url:
            raise OSError("HTTP Error 504: Gateway Time-out")
        return b"data"

    monkeypatch.setattr(updater, "_get", get)
    data = updater._download("https://api.github.com/repos/ssjrocks/sdr-toolkit/releases/assets/1",
                             "https://github.com/ssjrocks/sdr-toolkit/releases/download/v1/x.json")
    assert data == b"data" and len(tried) == 2
