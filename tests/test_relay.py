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
    assert "from sdr_toolkit.relay_stream import main" in up[3]
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
