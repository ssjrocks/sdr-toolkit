"""The toolkit's RTL-SDR modes through SDR Tools Relay: the relay (from ../sdr-tools-relay, or
SDR_TOOLS_RELAY_SRC) plays each mode's test recording as its "dongle", and the real decoders
receive it through the window's bridge, as they would from a relay on another PC."""

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RELAY_SRC = Path(os.environ.get("SDR_TOOLS_RELAY_SRC", HERE.parents[1] / "sdr-tools-relay" / "src"))
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]
if RELAY_SRC.is_dir():
    sys.path.append(str(RELAY_SRC))

import radio_signals  # noqa: E402
from sdr_toolkit import relay  # noqa: E402
from sdr_toolkit.modes import MODES  # noqa: E402

BY_ID = {M.id: M for M in MODES}
sdr_relay = pytest.importorskip("sdr_relay", reason="needs the sdr-tools-relay checkout beside this one")
from sdr_relay import protocol as P  # noqa: E402
from sdr_relay.discovery import Responder  # noqa: E402
from sdr_relay.rtlsdr import FileDongle  # noqa: E402
from sdr_relay.server import Config, Relay  # noqa: E402


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def make_relay():
    started = []

    def make(recording, password="pw"):
        r = Relay(Config(name="Test relay", port=free_port(), password=password, file=str(recording)), version="t")
        r.dongles = []

        def opener():
            d = FileDongle(recording)
            r.dongles.append(d)
            return d

        r.open_dongle = opener
        r.start()
        started.append(r)
        return r

    yield make
    relay.disconnect()
    for r in started:
        r.stop()


def run_live(mode_id, until, wait=40, **values):
    """Run a mode on "RTL-SDR" input (the relay, through the bridge) until until(events) or timeout."""
    mode = BY_ID[mode_id]()
    v = dict(mode.values)
    v.update(values, input="rtl")
    mode.start(v)
    events = []
    end = time.time() + wait
    try:
        while time.time() < end:
            time.sleep(0.3)
            events += mode.poll()
            if until(events):
                break
    finally:
        events += mode.stop()
    return mode, events


def test_connect_checks_password_and_busy(make_relay, tmp_path):
    rec = tmp_path / "x.cu8"
    rec.write_bytes(bytes(256) * 100)
    r = make_relay(rec)
    with pytest.raises(relay.RelayError, match="needs a password"):
        relay.connect("127.0.0.1", r.config.port, "")
    with pytest.raises(relay.RelayError, match="wrong password"):
        relay.connect("127.0.0.1", r.config.port, "nope")
    b = relay.connect("127.0.0.1", r.config.port, "pw")
    assert relay.current() is b and b.name == "Test relay" and b.local_port
    # Through the bridge it's plain rtl_tcp, with no password.
    s = socket.create_connection(("127.0.0.1", b.local_port), timeout=5)
    s.sendall(P.command(P.SET_FREQ, 433920000))
    head = s.recv(12)
    assert head[:4] == b"RTL0"
    time.sleep(0.3)
    assert ("set_center_freq", 433920000) in r.dongles[-1].calls
    with pytest.raises(relay.RelayError, match="in use"):
        relay.open_session("127.0.0.1", r.config.port, "pw")
    s.close()
    relay.disconnect()
    assert relay.current() is None
    with pytest.raises(relay.RelayError, match="Couldn't reach"):
        relay.connect("127.0.0.1", free_port(), "")


def test_lan_scan(make_relay, tmp_path):
    rec = tmp_path / "x.cu8"
    rec.write_bytes(bytes(256))
    r = make_relay(rec)
    port = free_port(socket.SOCK_DGRAM)
    resp = Responder(r, port)
    resp.start()
    try:
        found = relay.discover(timeout=1.0, targets=["127.0.0.1"], port=port)
        assert len(found) == 1 and found[0]["host"] == "127.0.0.1" and found[0]["port"] == r.config.port
        assert found[0]["name"] == "Test relay" and found[0]["password"]
    finally:
        resp.stop()
    assert "255.255.255.255" in relay.broadcast_addresses()


def test_ism_through_relay(make_relay, tmp_path):
    rec = radio_signals.save(tmp_path / "ism.cu8", radio_signals.nexus_iq(rate=250000))
    r = make_relay(rec)
    relay.connect("127.0.0.1", r.config.port, "pw")
    _, ev = run_live("ism", lambda ev: any(e.kind == "row" for e in ev), freqs="433.92M", rate="250k")
    rows = [e.data for e in ev if e.kind == "row"]
    assert rows, [e.text for e in ev if e.kind in ("line", "raw")][-10:]
    assert "Nexus" in json.dumps(rows[0]) and ("set_center_freq", 433920000) in r.dongles[-1].calls


def test_adsb_through_relay(make_relay, tmp_path):
    rec = radio_signals.save(tmp_path / "adsb.cu8", radio_signals.adsb_iq(0x7C1234, "QFA123", -33.95, 151.18, 12000))
    r = make_relay(rec)
    relay.connect("127.0.0.1", r.config.port, "pw")

    def found(ev):
        return any(e.kind == "table" and any(x["flight"] == "QFA123" and x["pos"] for x in e.data["rows"]) for e in ev)

    mode, ev = run_live("adsb", found, lat="-33.87", lon="151.21")
    assert found(ev), [e.text for e in ev if e.kind in ("line", "raw")][-10:]
    assert ("set_center_freq", 1090000000) in r.dongles[-1].calls and r.dongles[-1].rate == 2400000
    assert mode.actions() == [] and "through the relay Test relay" in mode.activity()


def test_ais_through_relay(make_relay, tmp_path):
    # The relay path tunes 60 kHz above the channel, so the channel sits 60 kHz below the centre.
    payloads = [radio_signals.ais_payload(mmsi=503123456 + k) for k in range(5)]
    rec = radio_signals.save(tmp_path / "ais.cu8", radio_signals.ais_iq(payloads=payloads, offset_hz=-60000))
    r = make_relay(rec)
    relay.connect("127.0.0.1", r.config.port, "pw")

    def found(ev):
        return any(e.kind == "table" and e.data["rows"] for e in ev)

    _, ev = run_live("ais", found)
    assert found(ev), [e.text for e in ev if e.kind in ("line", "raw")][-10:]
    assert ("set_center_freq", 162035000) in r.dongles[-1].calls and r.dongles[-1].rate == 240000


def test_digital_voice_and_gps_use_the_bridge(make_relay, tmp_path):
    rec = tmp_path / "x.cu8"
    rec.write_bytes(bytes(256))
    r = make_relay(rec)
    b = relay.connect("127.0.0.1", r.config.port, "pw")
    dv = BY_ID["dmr"]()
    dv.values = dict(dv.values, input="rtl", freq_mhz="438.8")
    try:
        cmd = dv.command()
        assert f"rtltcp:127.0.0.1:{b.local_port}:438.8M:" in " ".join(cmd)
    except Exception as e:                       # DSD-FME not built here
        assert "dsd-fme" in str(e)
    gps = BY_ID["gps"]()
    gps.values = dict(gps.values, input="rtl", ppm=57)
    cmd = gps.command()
    conf = (Path(gps._dir) / "gps.conf").read_text()
    assert "Fifo_Signal_Source" in conf and "Acquisition_1C.blocking=false" in conf
    assert "InputFilter.IF=0" in conf                       # the relay corrects its own dongle
    up = gps.upstream()
    assert up[up.index("--format") + 1] == "s8" and up[up.index("--freq") + 1] == "1575420000"
    assert "sdr_toolkit.relay_stream" in " ".join(up[:4])         # system.python_command runs it
    import shutil
    shutil.rmtree(gps._dir, ignore_errors=True)


def test_lora_through_relay(make_relay, tmp_path):
    import test_modes as TM
    if TM.lora_available() is None:
        pytest.skip("gr-lora_sdr isn't built")
    from sdr_toolkit.engines import mesh
    frame = mesh.build_meshtastic(0xA1B2C3D4, 1001, mesh.pb_encode([(1, 1), (2, "Hello through the relay")]),
                                  mesh.DEFAULT_PSK, 8)
    rec = TM.lora_recording(tmp_path / "mt.cu8", [frame], 1000000, 250000, 11, 1, "0x2B", 16, 919.875e6)
    r = make_relay(rec)
    relay.connect("127.0.0.1", r.config.port, "pw")

    def found(ev):
        return any("Hello through the relay" in e.text for e in ev if e.kind == "line")

    _, ev = run_live("lora", found, wait=60, preset="mt:LongFast", region="ANZ")
    assert found(ev), [e.text for e in ev if e.kind in ("line", "raw")][-10:]
    assert r.dongles[-1].rate == 1000000 and ("set_center_freq", 919875000) in r.dongles[-1].calls


def test_connect_and_mode_switches_arent_turned_away(make_relay, tmp_path):
    # Seen on Windows: "in use by someone else" straight after Connect, and after switching
    # modes, while the relay was still closing the dongle of the connection before.
    from sdr_relay.rtlsdr import FileDongle as FD
    rec = tmp_path / "x.cu8"
    rec.write_bytes(bytes(range(256)) * 64)
    r = make_relay(rec)

    class SlowClose(FD):
        def close(self):
            time.sleep(1.5)
            super().close()

    def opener():
        d = SlowClose(rec)
        r.dongles.append(d)
        return d

    r.open_dongle = opener
    b = relay.connect("127.0.0.1", r.config.port, "pw")
    assert r.dongles == []                     # the password check doesn't open the dongle
    for _ in range(2):                         # a decoder, then the next mode's decoder straight after
        s = socket.create_connection(("127.0.0.1", b.local_port), timeout=10)
        s.sendall(P.command(P.SET_RATE, 250000))
        head = b""
        while len(head) < 12:
            chunk = s.recv(12 - len(head))
            assert chunk, b.last_error
            head += chunk
        assert head[:4] == b"RTL0"
        s.close()
    assert len(r.dongles) == 2 and b.last_error == ""


def test_update_a_connected_relay(make_relay, tmp_path, monkeypatch):
    rec = tmp_path / "x.cu8"
    rec.write_bytes(bytes(256))
    r = make_relay(rec)
    asked = []

    def on_update(who):
        asked.append(who)
        r.version = "9.9.9"                     # as if it had updated and restarted
        return True, "9.9.9"

    r.on_update = on_update
    b = relay.connect("127.0.0.1", r.config.port, "pw")
    assert b.relay_version == "t" and "update" in b.features
    assert relay.request_update("127.0.0.1", r.config.port, "pw") == "9.9.9" and asked == ["127.0.0.1"]
    assert relay.wait_for_version("127.0.0.1", r.config.port, "9.9.9", timeout=3, every=0.2)
    with pytest.raises(relay.RelayError, match="wrong password"):
        relay.request_update("127.0.0.1", r.config.port, "nope")
    r.on_update = None                          # an older relay: no "update" feature
    with pytest.raises(relay.RelayError, match="can't be updated from here"):
        relay.request_update("127.0.0.1", r.config.port, "pw")
    assert relay.parse_version("0.1.10") > relay.parse_version("0.1.9")


def test_a_relay_whose_dongle_sends_nothing(make_relay, tmp_path):
    # As on the Windows PC in the problem report: the header, then no samples at all.
    from sdr_relay.rtlsdr import FileDongle as FD
    rec = tmp_path / "x.cu8"
    rec.write_bytes(bytes(256))
    r = make_relay(rec)

    class Silent(FD):
        def start(self, on_data, buf_len=65536, method="auto"):
            pass

    r.open_dongle = lambda: Silent(rec)
    relay.connect("127.0.0.1", r.config.port, "pw")
    mode, ev = run_live("adsb", lambda ev: any("no samples" in (e.text or "") for e in ev), wait=25)
    warn = [e.text for e in ev if e.kind == "line" and e.style == "warn"]
    assert any("sent no samples for 10 s" in t for t in warn), [e.text for e in ev if e.kind in ("line", "raw")][-6:]
    for _ in range(50):                       # and the relay is free again
        if r.session is None:
            break
        time.sleep(0.1)
    assert r.session is None


HACKRF = {"index": 0, "id": "hackrf:000000000000000090186", "type": "hackrf", "name": "HackRF One",
          "serial": "000000000000000090186"}


def relay_with_devices(make_relay, tmp_path, devices):
    """A relay whose window is set to RTL-SDR 0, with these devices plugged in: opening one that
    isn't fails as the real relay does ("Couldn't open the RTL-SDR (error -1)")."""
    from sdr_relay.rtlsdr import RtlError
    rec = tmp_path / "x.cu8"
    rec.write_bytes(bytes(2400000))
    r = make_relay(rec)
    r.config.device = "rtl:0"
    r.describe_devices = lambda: devices
    r.opened = []

    def opener(device=None):
        r.opened.append(device or r.config.device)
        if (device or r.config.device) not in [d["id"] for d in devices]:
            raise RtlError("Couldn't open the RTL-SDR (error -1). Close any other program using it.")
        return FileDongle(rec)

    r.open_dongle = opener
    return r


def test_a_relay_set_to_an_absent_dongle_uses_the_hackrf_it_has(make_relay, tmp_path):
    # As in the problem report: relay 0.2.0 still set to "RTL-SDR 0", with only a HackRF plugged in.
    r = relay_with_devices(make_relay, tmp_path, [HACKRF])
    b = relay.connect("127.0.0.1", r.config.port, "pw")
    assert b.device_id == HACKRF["id"] and b.want_device == HACKRF["id"] and b.device["type"] == "hackrf"
    mode, ev = run_live("adsb", lambda ev: r.opened, wait=10)
    assert r.opened and set(r.opened) == {HACKRF["id"]}
    assert not [e.text for e in ev if e.kind == "line" and e.style == "warn" and "open" in (e.text or "")]


def test_picked_device_and_the_relays_own_choice():
    rtl = {"id": "rtl:0", "type": "rtl", "name": "RTL-SDR"}
    assert relay.choose_device([rtl, HACKRF], "rtl:0") == "rtl:0"                 # its own, plugged in
    assert relay.choose_device([rtl, HACKRF], "rtl:0", HACKRF["id"]) == HACKRF["id"]   # picked here
    assert relay.choose_device([rtl], "rtl:0", HACKRF["id"]) == "rtl:0"           # picked, but gone
    assert relay.choose_device([{"index": 0, "name": "old relay"}], "0") == "0"  # relays before 0.2.0
    b = relay.Bridge("h", 1)
    b.devices = [rtl, HACKRF]
    b.use_device("rtl:0", "rtl:0")
    assert b.want_device is None and b.device["type"] == "rtl"       # nothing to ask for: older relays too


def test_a_refused_stream_says_the_relays_reason(make_relay, tmp_path):
    r = relay_with_devices(make_relay, tmp_path, [])
    relay.connect("127.0.0.1", r.config.port, "pw")
    mode, ev = run_live("adsb", lambda ev: any("Couldn't open" in (e.text or "") for e in ev), wait=10)
    warn = [e.text for e in ev if e.kind == "line" and e.style == "warn"]
    assert any("Test relay: Couldn't open the RTL-SDR" in t for t in warn), warn
    assert not any("Errno" in t for t in warn), warn


def test_relay_dongle_check(make_relay, tmp_path):
    rec = tmp_path / "x.cu8"
    rec.write_bytes(bytes(256))
    r = make_relay(rec)
    b = relay.connect("127.0.0.1", r.config.port, "pw")
    assert relay.diagnose("127.0.0.1", r.config.port, "pw") == {"skipped": "playing a recording"}
    assert "diagnose" in b.features
    with pytest.raises(relay.RelayError, match="wrong password"):
        relay.diagnose("127.0.0.1", r.config.port, "nope")


def test_which_relays_count_as_this_network():
    assert relay.is_lan("192.168.1.243") and relay.is_lan("10.0.0.5") and relay.is_lan("127.0.0.1")
    assert relay.is_lan("localhost")
    assert not relay.is_lan("100.66.93.15")          # Tailscale: could be anywhere
    assert not relay.is_lan("130.185.249.109") and not relay.is_lan("no-such-host.invalid")


def test_receiver_drives_a_relays_dongle(make_relay, tmp_path, monkeypatch):
    import numpy as np
    import test_receiver as TR
    rec = TR.fm_station(tmp_path / "fm.cu8", offset=600000, seconds=1.0)
    r = make_relay(rec)
    r.config.allow_client_ppm = True
    relay.connect("127.0.0.1", r.config.port, "pw")
    m = TR.start_receiver(monkeypatch, input="rtl", freq_mhz=101.1, gain_mode="manual", gain_db=37.2, rtl_agc=True,
                          direct=2, bias_tee=True, ppm=-3, volume=100)
    try:
        ev = TR.poll_for(m, 2.5)
        calls = r.dongles[-1].calls
        for want in (("set_sample_rate", 2400000), ("set_center_freq", 100500000), ("set_tuner_gain_mode", 1),
                     ("set_tuner_gain", 372), ("set_agc_mode", 1), ("set_direct_sampling", 2),
                     ("set_bias_tee", 1), ("set_freq_correction", -3)):
            assert want in calls, (want, calls)
        assert [e for e in ev if e.kind == "spectrum"] and "relay Test relay" in m._status()[0].text
        audio = np.concatenate(TR.FakeAudio.last.chunks)
        assert abs(TR.tone_freq(audio) - 1000) < 5                 # the FM station, through the relay
        m.apply(dict(m.values, gain_mode="auto", bias_tee=False, freq_mhz=145.0))
        time.sleep(0.3)
        assert ("set_tuner_gain_mode", 0) in calls and ("set_bias_tee", 0) in calls
        assert ("set_center_freq", int(145e6 - 600000)) in calls
    finally:
        m.stop()


def test_dmr_at_19_2_ms_s_from_a_live_stream_keeps_up(make_relay, tmp_path, monkeypatch):
    """The explorer on a HackRF at 19.2 MS/s, from a stream that doesn't wait (a relay playing in real time):
    the receiver muted, four DMR points (one on a signal 2.4 MHz off the centre). Nothing may be dropped,
    before the channels or in them, and the signal decodes. At first the receiver did 1.1 s of work for
    each second of signal here, fell behind, and dropped blocks: CRC error after CRC error."""
    import numpy as np
    import test_receiver as TR
    from sdr_toolkit.modes.digital_voice import dsd_fme
    if dsd_fme() is None:
        pytest.skip("DSD-FME isn't built")
    import signals
    rate, offset = 19_200_000, 2_400_000
    a = signals.dmr_bursts(48000, 6.0).astype(np.float64)
    rec = tmp_path / "wide.cu8"
    phase = 0.0
    rng = np.random.default_rng(1)
    with open(rec, "wb") as f:
        for i in range(0, len(a), 12000):
            dev = np.repeat(a[i:i + 12000], rate // 48000)
            ph = phase + 2 * np.pi * np.cumsum(offset + 1944 * dev) / rate
            phase = float(ph[-1])
            x = 0.4 * np.exp(1j * ph) + 0.01 * (rng.standard_normal(len(dev)) + 1j * rng.standard_normal(len(dev)))
            iq = np.empty(2 * len(x), np.float32)
            iq[0::2], iq[1::2] = x.real, x.imag
            np.clip(np.rint(iq * 127.5 + 127.5), 0, 255).astype(np.uint8).tofile(f)
    r = make_relay(rec)
    relay.connect("127.0.0.1", r.config.port, "pw")
    m = TR.start_receiver(monkeypatch, input="rtl", rate=rate, freq_mhz=101.1, mute=True)
    try:
        TR.poll_for(m, 1.0)
        c = m.center
        for off in (offset, -1_000_000, 4_000_000, -6_000_000):
            m.add_point(c + off, "dmr")
        frames = errors = 0
        t0 = time.time()
        while time.time() < t0 + 15:
            for e in TR.poll_for(m, 0.5):
                if (e.kind == "point_decoded" and e.data["event"].kind == "dsd"
                        and round(e.data["freq"] - c) == offset):
                    if e.data["event"].data.get("error"):
                        errors += 1
                    else:
                        frames += 1
        lost = getattr(m, "_chz_dropped", 0)
        assert m.src.dropped == 0 and lost == 0 and frames >= 20 and errors <= frames // 4, (
            m.src.dropped, lost, frames, errors)
    finally:
        m.stop()
