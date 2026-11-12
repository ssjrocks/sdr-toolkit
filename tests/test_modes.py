"""Every mode, fed a test signal through the same interface the window uses.

    python3 -m pytest tests
"""

import os
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


def test_aprs_bit_repair_advice_is_not_a_warning():
    # direwolf 1.8 prints advice about FIX_BITS that ends "...and you see messages like "Audio input
    # device 0 error code -32: Broken pipe""; it showed as a warning and in the app log.
    from sdr_toolkit.modes.process import looks_like_error
    assert BY_ID["aprs"]().values["fix_bits"] == 0
    a = signals.aprs_packets(48000)
    _, ev = run("aprs", pad(a, 48000), fix_bits=1)
    assert len([t for t in lines(ev) if "WB2OSZ-15>TEST" in t]) == 4
    assert not [e.text for e in ev if e.kind == "line" and e.style == "warn"]
    assert not looks_like_error("Position Error: Less than 2m; Reserved: 7; TE: < 5s;")    # DSD-FME's LRRP


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


def _ships(ev):
    tables = [e.data["rows"] for e in ev if e.kind == "table" and e.data["rows"]]
    return {r["mmsi"]: r for r in tables[-1]} if tables else {}


def test_ais(tmp_path):
    payloads = [radio_signals.ais_payload(mmsi=503123456 + k) for k in range(5)]
    path = radio_signals.save(tmp_path / "ais.cu8", radio_signals.ais_iq(payloads=payloads))
    _, ev = run_radio("ais", path, file_rate=240000, lat="-33.87", lon="151.21")
    ships = _ships(ev)
    # Bursts start abruptly out of noise; direwolf misses about 1 in 20 of these at any level.
    assert len(ships) >= 4, ships
    s = ships.get(503123456) or next(iter(ships.values()))
    assert s["pos"] == "-33.85000, 151.21000" and s["speed"] == "12.3" and s["course"] == "87"
    assert s["dist"] == "2.2 km"                    # from the location given
    points = [e.data for e in ev if e.kind == "points"][-1]
    assert points["home"] == (-33.87, 151.21) and len(points["points"]) == len(ships)
    assert any("NMEA" in e.text or "!AIVDM" in e.text for e in ev if e.kind == "raw")
    assert [e for e in ev if e.kind == "log"]


def test_ais_names_from_static_reports(tmp_path):
    payloads = [radio_signals.ais_static_payload(name="SPIRIT OF TEST"), radio_signals.ais_payload()]
    path = radio_signals.save(tmp_path / "ais.cu8", radio_signals.ais_iq(payloads=payloads))
    _, ev = run_radio("ais", path, file_rate=240000)
    s = _ships(ev)[503123456]
    assert s["name"] == "SPIRIT OF TEST" and s["type"] == "cargo" and s["pos"]
    assert any(e.data["points"] and e.data["points"][0]["label"] == "SPIRIT OF TEST"
               for e in ev if e.kind == "points")


def test_ais_channel_offset(tmp_path):
    # The AIS channel 50 kHz off the recording's centre.
    path = radio_signals.save(tmp_path / "ais.cu8", radio_signals.ais_iq(repeats=2, offset_hz=50000))
    _, ev = run_radio("ais", path, file_rate=240000, file_offset=50000)
    assert _ships(ev)


def test_ais_decoder():
    from sdr_toolkit.engines import ais
    a = ais.Assembler()
    # The example in the AIVDM documentation (gpsd): MMSI 366053209 in San Francisco Bay.
    r = a.add("!AIVDM,1,1,,B,15M67FC000G?ufbE`FepT@3n00Sa,0*5C")
    assert r.mmsi == 366053209 and abs(r.fields["lat"] - 37.80212) < 1e-4 and abs(r.fields["lon"] + 122.34162) < 1e-4
    assert a.add("!AIVDM,1,1,,B,15M67FC000G?ufbE`FepT@3n00Sa,0*5D") is None      # bad checksum
    # A two-sentence type 5 is joined before decoding.
    bits = "".join(map(str, radio_signals.ais_static_payload(name="HARBOUR CAT", callsign="VK2XYZ")))
    first, second = ais.encode_sentences(bits, seq="7")
    assert a.add(first) is None
    r = a.add(second)
    assert r.kind == 5 and r.fields["name"] == "HARBOUR CAT" and r.fields["callsign"] == "VK2XYZ"


def _dongle_present():
    import subprocess
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    except OSError:             # no lsusb (Windows, minimal containers): can't tell, assume none
        return False
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
def test_dmr_frames_show_before_any_call(tmp_path):
    # DSD-FME's call log only fills once a call has been decoded; until then the frame syncs say a
    # DMR signal is there (slot, colour code), and a summary follows when it ends.
    import wave
    a = signals.dmr_bursts(48000, 4.0) * 0.4
    a = a + np.random.default_rng(2).normal(0, 0.01, len(a))
    path = tmp_path / "dmr.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes((a * 32767).astype("<i2").tobytes())
    mode, ev = run_radio("dmr", path, wait=60, voice=False)
    out = [e.text for e in ev if e.kind == "line" and e.style != "dim"]
    heard = [t for t in out if " DMR signal: " in t]
    ended = [t for t in out if " DMR ended: " in t]
    assert len(heard) == 1 and "slot" in heard[0] and "colour code" in heard[0], out
    assert len(ended) == 1 and "frames" in ended[0], out


def test_dmr_voice_volume_sets_its_stream(monkeypatch):
    from sdr_toolkit.modes import digital_voice as dv
    calls = []
    monkeypatch.setattr(dv, "stream_node", lambda pid: 77 if pid == 4242 else None)
    monkeypatch.setattr(dv.shutil, "which", lambda prog: "/usr/bin/" + prog)
    monkeypatch.setattr(dv.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    m = BY_ID["dmr"]()
    m.values = dict(m.values, voice=True)
    m._proc = type("P", (), {"pid": 4242, "poll": lambda self: None})()
    m.apply(dict(m.values, voice_volume=40))
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls == [["wpctl", "set-volume", "77", "0.40"]]


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
    monkeypatch.setattr(updater, "can_install", lambda: True)          # as if installed (venv, or the Windows installer's)
    monkeypatch.setattr(updater, "_download", lambda *a: b"not the real wheel")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    ran = []
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: ran.append(a))   # Windows: the installer
    u = updater.Update("9.9.9", "https://api.github.com/repos/ssjrocks/sdr-toolkit/releases/assets/1",
                       "sdr_toolkit-9.9.9-py3-none-any.whl", "0" * 64)
    with pytest.raises(RuntimeError, match="checksum"):
        updater.install(u)
    assert not ran                                   # neither pip nor the installer ran


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


def test_ism_activity_line(tmp_path):
    path = radio_signals.save(tmp_path / "nexus.cu8", radio_signals.nexus_iq())
    mode, ev = run_radio("ism", path)
    line = mode.activity()
    assert line.startswith("Listening on the recording") and "1 decoded" in line, line


def test_adsb_map_points(tmp_path):
    path = radio_signals.save(tmp_path / "adsb.cu8", radio_signals.adsb_iq(0x7C1234, "QFA123", -33.95, 151.18, 12000))
    _, ev = run_radio("adsb", path, lat="-33.87", lon="151.21")
    pts = [e.data for e in ev if e.kind == "points" and e.data["points"]]
    assert pts and pts[-1]["home"] == (-33.87, 151.21)
    p = pts[-1]["points"][0]
    assert p["kind"] == "aircraft" and p["label"].startswith("QFA123") and abs(p["lat"] + 33.95) < 0.001
    assert any("QFA123" in e.text for e in ev if e.kind == "log")


def test_adsb_message_rate_and_missed_share(tmp_path):
    import json
    m = BY_ID["adsb"]()
    m.values = dict(m.values, ttl=60, units="km", lat="", lon="")
    m._dir = tmp_path
    for attr, value in (("_known", {}), ("_tracks", {}), ("_pos_t", {}), ("_counts", {}), ("_logged", {}),
                        ("_ppm_readings", []), ("_ppm_last", None)):
        setattr(m, attr, value)
    t0 = 1_800_000_000.0
    for i in range(41):                                   # 40 s of readsb's aircraft.json, a second apart
        now = t0 + i
        (tmp_path / "aircraft.json").write_text(json.dumps({"now": now, "messages": 0, "aircraft": [
            {"hex": "7c0001", "type": "adsb_icao", "messages": int(3.1 * i), "seen": 0.1, "alt_baro": 30000},
            {"hex": "7c0002", "type": "adsb_icao", "messages": 7 * i, "seen": 0.1, "alt_baro": 3000},
            {"hex": "7c0003", "type": "mode_s", "messages": 2 * i, "seen": 0.2}]}))
        ev = m._read_json()
    rows = {r["hex"]: r for e in ev if e.kind == "table" for r in e.data["rows"]}
    assert abs(float(rows["7C0001"]["rate"]) - 3.1) < 0.15 and rows["7C0001"]["missed"] in ("49%", "50%", "51%")
    assert rows["7C0002"]["missed"] == "0%"                # radar replies on top: more than it squitters
    assert rows["7C0003"]["rate"] == "2.0" and rows["7C0003"]["missed"] == ""     # Mode S only: no estimate
    cols = [k for k, _ in m.columns]
    assert cols.index("rate") == cols.index("msgs") + 1 and cols.index("missed") == cols.index("rate") + 1


def test_big_tables_fill_quickly():
    """Refilling a big table whose columns fit their contents took seconds (each cell re-measured its
    column over every row): the Satellite tracker's list froze the window."""
    from PyQt6.QtWidgets import QApplication, QHeaderView, QTableWidget
    from sdr_toolkit.widgets import SortItem, filling, sortable
    app = QApplication.instance() or QApplication([])
    t = sortable(QTableWidget(0, 6))
    for i in range(6):
        t.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
    start = time.perf_counter()
    for rnd in range(2):
        with filling(t):
            t.setRowCount(1500)
            for r in range(1500):
                for c in range(6):
                    t.setItem(r, c, SortItem(f"{r * (c + 1) + rnd} s"))
        app.processEvents()
    assert time.perf_counter() - start < 1.0
    assert t.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents


# ---- Radiosondes (rs1729's decoders: setup/build.sh, or build/ext/RS here) -------------------------

SONDE_WAV = Path(__file__).resolve().parent / "data" / "sondes" / "rs41_402mhz_40s.wav"
RS_BUILD = Path(__file__).resolve().parents[1] / "build" / "ext" / "RS"


def _sonde_tools(monkeypatch):
    """The decoders: the app's install, or the build in build/ext/RS (on PATH for the engine)."""
    from sdr_toolkit.engines import sonde_rx
    if (RS_BUILD / "demod" / "mod" / "rs41mod").is_file():
        monkeypatch.setenv("PATH", f"{RS_BUILD / 'demod' / 'mod'}:{RS_BUILD / 'scan'}:{os.environ['PATH']}")
    if sonde_rx.program("rs41mod") is None or sonde_rx.program("dft_detect") is None:
        pytest.skip("rs1729's radiosonde decoders aren't built (setup/build.sh)")


def sonde_iq(path, sondes, rate=240000, seconds=40.0, centre=402e6):
    """A cu8 recording with the real RS41 recording FM-modulated onto each of `sondes` (Hz), in noise."""
    import wave
    from scipy.signal import resample_poly
    w = wave.open(str(SONDE_WAV))
    a = (np.frombuffer(w.readframes(int(48000 * seconds)), np.uint8).astype(np.float32) - 128) / 128
    a = resample_poly(a / np.percentile(np.abs(a), 99.5), rate // 48000, 1)
    t = np.arange(len(a)) / rate
    iq = np.random.default_rng(2).normal(0, 0.02, (len(a), 2)) @ [1, 1j]
    for f in sondes:
        iq = iq + 0.25 * np.exp(1j * (2 * np.pi * (f - centre) * t + 2 * np.pi * 2400 * np.cumsum(a) / rate))
    cu8 = np.empty(2 * len(iq), np.uint8)
    cu8[0::2] = np.clip(iq.real * 127.5 + 127.5, 0, 255)
    cu8[1::2] = np.clip(iq.imag * 127.5 + 127.5, 0, 255)
    cu8.tofile(path)
    return path


def test_sonde_engine_finds_identifies_and_decodes_two_at_once(tmp_path, monkeypatch):
    _sonde_tools(monkeypatch)
    from sdr_toolkit.engines import sonde_rx
    from sdr_toolkit.receiver import dsp
    path = sonde_iq(tmp_path / "sondes.cu8", (402.050e6, 401.930e6))
    ev = []
    eng = sonde_rx.Engine(240000, 402e6, out=lambda **e: ev.append(e))
    data = path.read_bytes()
    for i in range(0, len(data), 48000):
        eng.feed(dsp.cu8(data[i:i + 48000]))
    eng.close()
    decoding = {e["freq"]: e["type"] for e in ev if e["event"] == "channel" and e["state"] == "decoding"}
    assert sorted(decoding) == [401930000.0, 402050000.0] and set(decoding.values()) == {"RS41"}, ev[:6]
    frames = [e for e in ev if e["event"] == "frame"]
    for f in decoding:
        mine = [e for e in frames if e["freq"] == f]
        assert len(mine) >= 25 and {e["id"] for e in mine} == {"K1930308"}, (f, len(mine))
    assert 28000 < frames[0]["alt"] < 29500 and abs(frames[0]["lat"] - 45.669) < 0.01


def test_sonde_mode_table_and_map(tmp_path, monkeypatch):
    _sonde_tools(monkeypatch)
    path = sonde_iq(tmp_path / "sonde.cu8", (402.050e6,))
    mode, ev = run_radio("sondes", path, wait=60, file_rate=240000, centre_mhz=402.0, lat="45.60", lon="15.80")
    rows = [r for e in ev if e.kind == "table" for r in e.data["rows"]]
    assert rows and rows[-1]["id"] == "K1930308" and rows[-1]["type"].startswith("RS41")
    last = rows[-1]
    assert last["freq"] == "402.050" and float(last["climb"]) > 0 and last["lands"] == "" and "km" in last["dist"]
    pts = [p for e in ev if e.kind == "points" for p in e.data["points"]]
    assert pts and pts[-1]["kind"] == "sonde" and len(pts[-1]["track"]) > 10
    assert not [p for p in pts if p["kind"] == "landing"]                 # still going up
    logs = [e.text for e in ev if e.kind == "log"]
    assert any("RS41, decoding" in t for t in logs) and any(t.startswith("K1930308") for t in logs)
    d = mode.details("K1930308")
    assert d["links"][0][1] == "https://sondehub.org/K1930308" and dict(d["fields"])["Type"].startswith("RS41")


def test_sonde_landing_estimate():
    from sdr_toolkit.modes.sondes import landing
    assert landing({"lat": -27.5, "lon": 153.0, "alt": 20000, "vel_v": 5.0, "vel_h": 10, "heading": 90}) is None
    lat, lon, secs = landing({"lat": -27.5, "lon": 153.0, "alt": 10100, "vel_v": -10.0, "vel_h": 10, "heading": 90},
                             ground_m=100)
    assert secs == 1000 and abs(lat + 27.5) < 0.01
    b, km = coverage.bearing_km((-27.5, 153.0), (lat, lon))
    assert abs(km - 10) < 0.05 and abs(b - 90) < 1


def test_table_sort_order():
    from sdr_toolkit.widgets import sort_key
    cells = ["10 s", "", "abc", "9 s", "-82.1", "7C1234", "3", "12.5 km at 30°", "Zulu", "2026-11-11 08:00"]
    assert sorted(cells, key=sort_key) == ["-82.1", "3", "9 s", "10 s", "12.5 km at 30°", "2026-11-11 08:00",
                                           "7C1234", "abc", "Zulu", ""]


def test_map_projection_and_fit():
    from PyQt6.QtWidgets import QApplication
    from sdr_toolkit.mapview import MapView, from_world, to_world
    for lat, lon in ((-33.87, 151.21), (51.5, -0.12), (0, 0)):
        x, y = to_world(lat, lon, 10)
        la, lo = from_world(x, y, 10)
        assert abs(la - lat) < 1e-9 and abs(lo - lon) < 1e-9
    app = QApplication.instance() or QApplication([])
    m = MapView(online=False)
    m.resize(800, 500)
    m.set_points([{"id": 1, "lat": -33.95, "lon": 151.18}, {"id": 2, "lat": -33.70, "lon": 151.40}],
                 home=(-33.87, 151.21))
    for la, lo in ((-33.95, 151.18), (-33.70, 151.40), (-33.87, 151.21)):
        pt = m._screen(la, lo)
        assert 0 <= pt.x() <= 800 and 0 <= pt.y() <= 500
    assert m.zoom >= 9                       # close in, not the whole world
    assert app is not None


def test_map_moves_markers_on_between_and_after_reports():
    from PyQt6.QtWidgets import QApplication
    from sdr_toolkit import mapview
    app = QApplication.instance() or QApplication([])
    m = mapview.MapView(online=False)
    home = (-33.87, 151.21)
    v = 450 * 1.852 / 3600                                  # km/s
    t0 = 1_800_000_000.0
    for i in range(10):                                     # a fix every second, flying north
        lat, lon = coverage.destination(home, 0.0, 20 + i * v)
        m.set_points([{"id": "7c1234", "kind": "aircraft", "lat": lat, "lon": lon, "pos_t": t0 + i,
                       "msg_t": t0 + i, "gs": 450, "course": 0.0, "heading": 0.0}], home)
    pt = m.points[0]
    last = t0 + 9
    # Between reports it moves on at its speed.
    at = m.live(pt, last + 0.5)
    assert abs(coverage.bearing_km(home, (at["lat"], at["lon"]))[1] - (20 + 9.5 * v)) < 0.01
    assert not at["predicted"]
    # Three missed updates (at its one a second, but at least 5 s): a prediction, still moving.
    assert not m.live(pt, last + 4)["predicted"]
    later = m.live(pt, last + 30)
    assert later["predicted"] and abs(coverage.bearing_km(home, (later["lat"], later["lon"]))[1]
                                      - (20 + 39 * v)) < 0.01
    assert later["fix"] == (pt["lat"], pt["lon"])
    # Messages still coming keep it; 60 s after the last one it's gone.
    pt["msg_t"] = last + 40
    assert m.current(last + 90) == [pt] and m.current(last + 101) == []
    # Without speed and course it stays put; a ship coasts on for longer.
    still = dict(pt, gs=None)
    assert m.live(still, last + 30) is still
    ship = {"id": 1, "kind": "ship", "lat": -33.9, "lon": 151.3, "pos_t": t0, "msg_t": t0, "gs": 10, "course": 90.0}
    m.points = [ship]
    assert m.current(t0 + 300) == [ship] and not m.live(ship, t0 + 60)["predicted"]
    assert m.live(ship, t0 + 120)["predicted"]
    assert app is not None


def test_adsb_ppm_from_readsb_measurements():
    """readsb's estimated_ppm is what's left after the correction already set; the button adds it on."""
    m = BY_ID["adsb"]()
    m.values.update(input="rtl", ppm=50)
    m._ppm_readings = []
    assert m.ppm_suggestion() is None and not m.actions()[0][2]          # measuring, button off
    m._ppm_readings = [7.2]
    assert m.ppm_suggestion() is None                                    # one reading isn't enough
    m._ppm_readings = [7.2, 6.8]
    assert m.ppm_suggestion() == (57, 2)
    aid, label, enabled, _ = m.actions()[0]
    assert enabled and "+57 ppm" in label and m.run_action(aid) == {"ppm": 57}
    m._ppm_readings = [7.2, 15.0]                                        # readings disagree: wait
    assert m.ppm_suggestion() is None
    m.values["ppm"] = 57
    m._ppm_readings = [0.3, -0.2]
    assert m.ppm_suggestion() == (57, 2) and not m.actions()[0][2]       # already right
    m.values["input"] = "file"
    assert m.actions() == []                                             # recordings can't measure it



# ---- GPS position fix

import gps_signal  # noqa: E402
from sdr_toolkit.engines import gnss  # noqa: E402


def test_gnss_corrections_and_output_lines():
    # A crystal 54 ppm fast samples fast and tunes high, so the signal lands below the centre.
    assert gnss.corrections(54) == (2048111, -85073)
    assert gnss.corrections(-51, 2000000) == (1999898, 80346)      # GNSS-SDR's RTL-SDR example: 80558
    rx = gnss.Receiver()
    assert rx.feed("Tracking of GPS L1 C/A signal started on channel 4 for satellite GPS PRN 12 (Block IIR-M)") == "track"
    assert rx.feed("New GPS NAV message received in channel 4: subframe 3 from satellite GPS PRN 12 "
                   "(Block IIR-M) with CN0=43 dB-Hz") == "nav"
    assert rx.sats[12].cn0 == 43 and rx.sats[12].subframes == 1 and rx.sats[12].tracking
    assert rx.feed("Loss of lock in channel 4!") == "lost" and not rx.tracking()
    assert rx.feed("New GPS NAV message received in channel 4: New GPS NAV message received in channel") is None
    assert rx.feed("Tracking of GPS L1 C/A signal started on channel 2 for satellite GPS PRN 00 (Block") is None
    rx.feed("Tracking of GPS L1 C/A signal started on channel 2 for satellite GPS PRN 11 (Block III)")
    assert rx.feed("\x1b[31mSatellite GPS PRN 11 (Block III) reports an unhealthy status, not used") == "unhealthy"
    assert not rx.sats[11].healthy


@pytest.mark.skipif(not shutil.which("gnss-sdr") or not gps_signal.simulator()[0],
                    reason="needs gnss-sdr and gps-sdr-sim (build/gps-sdr-sim)")
def test_gps_fix_from_a_dongle_54_ppm_off(tmp_path):
    # From cold: about 50 s to the first fix (the satellites' order varies run to run), then 5 to average.
    path = gps_signal.recording(tmp_path / "gps.cu8", -33.8688, 151.2093, seconds=120, ppm=54)
    mode, ev = run_radio("gps", path, wait=120, ppm=54)
    settings = [e.data for e in ev if e.kind == "setting"]
    raw = [e.text for e in ev if e.kind == "raw" and "receiver time" not in e.text]
    if not settings and os.environ.get("GPS_DEBUG"):
        Path(os.environ["GPS_DEBUG"]).write_text("\n".join(e.text for e in ev if e.kind == "raw"))
    assert settings, ([e.text for e in ev if e.kind == "line"], len(raw), raw[:12], raw[-6:])
    lat, lon = float(settings[-1]["lat"]), float(settings[-1]["lon"])
    assert abs(lat + 33.8688) < 0.0002 and abs(lon - 151.2093) < 0.0002          # within about 20 m
    assert mode.values["lat"] == settings[-1]["lat"]
    rows = [e.data["rows"] for e in ev if e.kind == "table" and e.data["rows"]][-1]
    assert sum(r["nav"] == "complete" for r in rows) >= 4
    assert [e for e in ev if e.kind == "points"][-1].data["home"]
    assert any("End of the recording" in e.text for e in ev if e.kind == "line")


# ---- logbook and coverage

from sdr_toolkit import coverage  # noqa: E402
from sdr_toolkit.logbook import Logbook  # noqa: E402


def test_logbook_entries_craft_and_positions(tmp_path):
    lb = Logbook(tmp_path / "log.sqlite")
    lb.add("ft8", "CQ VK2ABC QF56", t=1000)
    lb.add("aprs", "VK2XYZ-9 position", t=1001)
    for i in range(30):                     # one second apart, moving 20 m each: stored every 10 s
        lb.see("adsb", "7c1234", "QFA1", {"type": "B738", "alt": 1000 + i}, -33.9 + i * 0.00018, 151.2,
               alt=1000 + i, label="QFA1", t=2000 + i)
    lb.see("adsb", "7c1234", "QFA1", {}, -33.0, 151.2, t=2000 + 31 * 60)      # back after 31 min
    lb.flush()
    assert [e[2] for e in lb.entries()] == ["VK2XYZ-9 position", "CQ VK2ABC QF56"]
    assert lb.entries(mode="ft8", search="VK2") == [(1000, "ft8", "CQ VK2ABC QF56")]
    c = lb.craft("adsb")[0]
    assert c["key"] == "7c1234" and c["name"] == "QFA1" and c["sightings"] == 2 and c["first"] == 2000
    assert c["info"]["type"] == "B738"                     # kept when a later update leaves it out
    pos = lb.positions("adsb", 0, 1e9)
    assert [p[1] for p in pos] == [2000, 2010, 2020, 2000 + 31 * 60]
    assert lb.time_range("adsb") == (2000, 2000 + 31 * 60) and lb.time_range("ais") is None
    for i in range(5):                      # a fast mover: 100 m a second is stored every 300 m
        lb.see("adsb", "fast", lat=-33.9 + i * 0.0009, lon=151.2, t=5000 + i)
    assert [p[1] for p in lb.positions("adsb", 5000, 6000)] == [5000, 5003]
    lb.clear("adsb")
    assert not lb.craft("adsb") and lb.entries()
    lb.close()
    assert Logbook(tmp_path / "log.sqlite").entries()      # it's on disk


def test_coverage_rings():
    home = (-33.87, 151.21)
    cov = coverage.Coverage(home)
    # Aircraft flying north out to 200 km: every 2 s out to 120 km, then every 20 s (patchy).
    t = 0.0
    for n in range(3):
        for km in range(5, 201):
            t += 2 if km <= 120 else 20
            if km > 120 and km % 2:
                continue
            lat, lon = coverage.destination(home, 2.0, km)
            cov.add(f"a{n}", t, lat, lon)
    mx, rel, est = cov.outlines()
    north = 0
    assert 199 < mx[north][1] < 201 and rel[north][1] == 120 and est is None
    assert mx[18][1] == 0 and rel[18][1] == 0                         # nothing heard to the south
    b, km = coverage.bearing_km(home, coverage.destination(home, 123.0, 77.0))
    assert abs(b - 123) < 0.01 and abs(km - 77) < 0.01
    again = coverage.Coverage(home, cov.data())
    assert again.outlines() == cov.outlines()
    assert "furthest 200 km" in cov.summary()


def test_coverage_estimates_where_aircraft_are_heard_without_positions():
    home = (-33.87, 151.21)
    cov = coverage.Coverage(home)
    kt = 450
    v = kt * 1.852 / 3600                            # km/s (0.23)
    # Outbound north: positions every second out to 150 km, then 90 s more of other messages.
    t0 = 1000.0
    for i in range(20):
        t = t0 + i
        lat, lon = coverage.destination(home, 0.5, 145.5 + i * v)
        cov.heard("out", t)
        cov.add("out", t, lat, lon, kt, 0.5)
    last_pos = t
    for i in range(1, 91):
        assert cov.heard("out", last_pos + i) in (True, False)
    mx, rel, est = cov.outlines()
    assert 149 < mx[0][1] < 150.5
    assert abs(est[0][1] - (mx[0][1] + 90 * v)) < 0.5   # its last message, about 21 km further on
    # Inbound from the east: heard for 60 s before its first position at 120 km, flying west.
    cov.heard("in", 5000.0)
    for i in range(30):
        cov.heard("in", 5000.0 + i * 2)
    for i in range(5):
        t = 5060.0 + i
        lat, lon = coverage.destination(home, 90.5, 120 - i * v)
        cov.heard("in", t)
        cov.add("in", t, lat, lon, kt, 270.5)
    mx, rel, est = cov.outlines()
    assert 119.9 < mx[9][1] < 120.1
    assert abs(est[9][1] - (120 + 60 * v)) < 0.5        # where it was at its first message
    # Short gaps, no speed, and too long a silence don't count.
    cov2 = coverage.Coverage(home)
    for i in range(5):
        lat, lon = coverage.destination(home, 180.5, 100 + i * v)
        cov2.heard("s", 10.0 + i)
        cov2.add("s", 10.0 + i, lat, lon, kt, 180.5)
    cov2.heard("s", 16.0)                                  # 2 s after its last position
    cov2.heard("s", 14.0 + coverage.MAX_DR + 5)            # longer than dead reckoning is good for
    assert cov2.outlines()[2] is None
    again = coverage.Coverage(home, cov.data())
    assert again.outlines() == cov.outlines()
    assert "heard to about" in cov.summary()


# ---- Details: photos and the aircraft database

from sdr_toolkit import aircraftdb, photos  # noqa: E402

ACDB = Path(__file__).resolve().parent.parent / "build" / "acdb" / "aircraft.csv.gz"


def test_photo_answers():
    # Shapes of real answers (2026-11): Planespotters by hex, Wikipedia search and summary.
    ps = {"photos": [{"id": "2003915", "thumbnail": {"src": "https://t.plnspttrs.net/x_t.jpg"},
                      "thumbnail_large": {"src": "https://t.plnspttrs.net/x_280.jpg"},
                      "link": "https://www.planespotters.net/photo/2003915/vh-vgf", "photographer": "TommyNG"}]}
    got = photos.planespotters(ps)
    assert got["image"].endswith("_280.jpg") and "TommyNG" in got["credit"] and got["link"].endswith("vh-vgf")
    assert photos.planespotters({"photos": []}) is None
    search = {"pages": [{"key": "Cessna", "thumbnail": {"mimetype": "image/svg+xml"}},
                        {"key": "Cessna_182_Skylane", "thumbnail": {"mimetype": "image/jpeg"}}]}
    assert photos.search_title(search) == "Cessna_182_Skylane"
    summ = {"title": "Cessna 182 Skylane", "thumbnail": {"source": "https://upload/x.jpg"},
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Cessna_182_Skylane"}}}
    assert photos.summary(summ)["link"].endswith("Cessna_182_Skylane") and not photos.summary(summ)["own"]
    assert photos.first_request({"kind": "aircraft", "hex": "7C1234", "type": "C182"})[1].endswith("/hex/7c1234")
    assert photos.type_request({"kind": "aircraft", "type": "CESSNA 182 Skylane"})[1].endswith(
        "CESSNA%20182%20Skylane%20aircraft&limit=5")
    assert photos.first_request({"kind": "ship", "type": "tug"})[1].endswith("/Tugboat")
    assert photos.first_request({"kind": "ship", "type": ""})[1].endswith("/Ship")


@pytest.mark.skipif(not ACDB.is_file(), reason="needs build/acdb/aircraft.csv.gz (tar1090-db)")
def test_adsb_details_with_the_aircraft_database(tmp_path, monkeypatch):
    (tmp_path / "sdr-toolkit").mkdir()
    shutil.copy(ACDB, tmp_path / "sdr-toolkit" / "aircraft.csv.gz")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert aircraftdb.present() and not aircraftdb.due()
    # readsb loads the database in the background as it starts: 2 s of quiet first, so it's loaded
    # before the aircraft is heard (heard first, it never gets a registration, which a busy PC showed).
    iq = radio_signals.adsb_iq(0x7C1234, "QFA123", -33.95, 151.18, 12000)
    path = radio_signals.save(tmp_path / "adsb.cu8", np.concatenate([np.zeros(2 * 2400000, iq.dtype), iq]))
    mode, ev = run_radio("adsb", path, lat="-33.87", lon="151.21")
    d = mode.details("7C1234")
    fields = dict(d["fields"])
    assert d["title"] == "QFA123" and fields["Registration"] == "VH-DVQ" and "C182" in fields["Type"]
    assert fields["Altitude"] == "12,000 ft" and d["photo"] == {"kind": "aircraft", "hex": "7c1234",
                                                                "type": "CESSNA 182 Skylane"}
    point = [e.data for e in ev if e.kind == "points" and e.data["points"]][-1]["points"][0]
    assert point["name"] == "QFA123" and point["info"]["r"] == "VH-DVQ" and point["alt"] == 12000
    assert mode.details("7c9999") is None


def test_ais_details(tmp_path):
    payloads = [radio_signals.ais_static_payload(name="SPIRIT OF TEST"), radio_signals.ais_payload()]
    path = radio_signals.save(tmp_path / "ais.cu8", radio_signals.ais_iq(payloads=payloads))
    mode, ev = run_radio("ais", path, file_rate=240000, lat="-33.87", lon="151.21")
    fields = dict(mode.details("503123456")["fields"])
    assert fields["IMO number"] == "9123456" and fields["Size"] == "150 × 20 m" and fields["Draught"] == "8.5 m"
    assert fields["ETA"] == "09/11 14:06 UTC" and fields["Distance"] == "2.2 km"
    assert all(e.style == "pos" for e in ev if e.kind == "log" and "SPIRIT OF TEST:" in e.text)


def test_rtl_sample_rates_for_every_lora_preset():
    from sdr_toolkit.modes.radio import rtl_rate, rtl_rate_ok
    assert rtl_rate(500000) == 1000000 and rtl_rate(1000000) == 1000000 and rtl_rate(250000) == 250000
    assert rtl_rate(2400000) == 2400000 and not rtl_rate_ok(500000)
    mode = BY_ID["lora"]()
    presets = [c for c, _ in next(s for s in mode.settings if s.key == "preset").choices]
    cases = [dict(preset=p) for p in presets] + [dict(preset="custom", bw=bw) for bw in (62500, 125000, 250000, 500000)]
    for case in cases:
        mode.values = dict(mode.values, input="rtl", **case)
        bw = mode.params()[2]
        up = mode.upstream()
        rate = int(up[up.index("-s") + 1])
        assert rtl_rate_ok(rate) and rate % (bw * 4) == 0, (case, rate)


@needs_lora
def test_lora_125_khz_from_1_mhz_samples(tmp_path):
    # LongModerate is 125 kHz wide: 4x is 500 kHz, which the dongle can't do, so it's
    # captured at 1 MHz and resampled. A recording at 1 MHz takes the same path.
    mode = BY_ID["lora"]()
    mode.values = dict(mode.values, preset="mt:LongModerate", region="ANZ")
    freq, sf, bw, cr, sync, pre = mode.params()
    assert (sf, bw, cr) == (11, 125000, 4)
    key = mesh.DEFAULT_PSK
    frames = [mesh.build_meshtastic(0xA1B2C3D4, 1003, mesh.pb_encode([(1, 1), (2, "Moderate hello")]), key,
                                    mesh.channel_hash("LongModerate", key))]
    path = lora_recording(tmp_path / "mod.cu8", frames, 1000000, bw, sf, cr, sync, pre, freq)
    _, ev = run_radio("lora", path, wait=120, preset="mt:LongModerate", region="ANZ", file_rate=1000000)
    assert '"Moderate hello"' in text(ev), text(ev)


def test_ppm_confirmation_is_not_an_error():
    # librtlsdr prints this when a frequency correction is set; it used to show as a failure.
    from sdr_toolkit.modes.process import looks_like_error
    line = "Tuner error set to 57 ppm."
    assert not looks_like_error(line) and looks_like_error("rtlsdr_demod_write_reg failed with -9")
    for mode_id in ("ism", "adsb", "ais", "aprs"):
        mode = BY_ID[mode_id]()
        mode.values = dict(mode.values)
        for attr, value in (("_ready", False), ("_problem", None), ("_noise", None), ("_decoded", 0), ("_seen", 0),
                            ("_banner", [])):
            setattr(mode, attr, value)
        assert not [e for e in mode.parse(line) if e.style == "warn"], mode_id
