"""Satellite tracking: orbits, passes and Doppler (checked against Skyfield when it's there), and
the catalogue of what satellites transmit."""

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("sgp4")         # in the app's own environment (uv run --with pytest python -m pytest)
from sdr_toolkit.satellites import orbit  # noqa: E402

ISS = ("1 25544U 98067A   26314.88456348  .00009976  00000-0  18692-3 0  9992",
       "2 25544  51.6322 294.1523 0007308  11.5147 348.6007 15.49305067594493")
EPOCH = 1794181500.0              # 2026-11-10, near the elements' epoch
HOME = (-27.47, 153.03, 0.03)     # Brisbane


def test_orbit_matches_skyfield():
    sf = pytest.importorskip("skyfield.api")
    ts = sf.load.timescale()
    s = sf.EarthSatellite(*ISS, "ISS", ts)
    me = sf.wgs84.latlon(HOME[0], HOME[1], elevation_m=HOME[2] * 1000)
    times = EPOCH + np.arange(0, 86400, 977.0)
    tr = orbit.track(orbit.satrec(*ISS), times, HOME)
    for i, t in enumerate(times):
        when = ts.from_datetime(__import__("datetime").datetime.fromtimestamp(t, __import__("datetime").timezone.utc))
        sub = sf.wgs84.subpoint_of(s.at(when))
        alt, az, dist, _, _, rate = (s - me).at(when).frame_latlon_and_rates(me)
        assert abs(tr.lat[i] - sub.latitude.degrees) < 0.02
        assert abs((tr.lon[i] - sub.longitude.degrees + 180) % 360 - 180) < 0.02
        assert abs(tr.el[i] - alt.degrees) < 0.02
        assert abs((tr.az[i] - az.degrees + 180) % 360 - 180) < 0.05 or tr.el[i] > 89
        assert abs(tr.range[i] - dist.km) < 0.5
        assert abs(tr.rate[i] - rate.km_per_s) < 0.002


def test_passes_match_skyfield():
    sf = pytest.importorskip("skyfield.api")
    import datetime
    ts = sf.load.timescale()
    s = sf.EarthSatellite(*ISS, "ISS", ts)
    me = sf.wgs84.latlon(HOME[0], HOME[1], elevation_m=HOME[2] * 1000)
    t0, t1 = (ts.from_datetime(datetime.datetime.fromtimestamp(x, datetime.timezone.utc)) for x in
              (EPOCH, EPOCH + 2 * 86400))
    when, kind = s.find_events(me, t0, t1, altitude_degrees=0.0)
    rises = [w.utc_datetime().timestamp() for w, k in zip(when, kind) if k == 0]
    peaks = [w.utc_datetime().timestamp() for w, k in zip(when, kind) if k == 1]
    mine = orbit.passes(orbit.satrec(*ISS), HOME, EPOCH, days=2)
    assert len(mine) == len(rises) and len(mine) >= 4
    for p, r, k in zip(mine, rises, peaks):
        assert abs(p.start - r) < 3 and abs(p.peak - k) < 3
        assert p.end > p.peak > p.start and 0 < p.max_el <= 90


def test_doppler_and_footprint():
    sat = orbit.satrec(*ISS)
    p = max(orbit.passes(sat, HOME, EPOCH, days=2), key=lambda p: p.max_el)
    tr = orbit.track(sat, np.arange(p.start, p.end, 1.0), HOME)
    shift = orbit.doppler(145.8e6, tr.rate) - 145.8e6
    assert shift[0] > 2500 and shift[-1] < -2500 and np.max(np.abs(shift)) < 3700   # ±3.5 kHz on 2 m
    assert abs(orbit.footprint_km(420) - 2250) < 100                   # what it can hear down to 0°
    assert orbit.footprint_km(420, 10) < orbit.footprint_km(420)


def test_daylight():
    # Local noon in Brisbane (UTC+10) on 2026-11-10: the Sun is high.
    noon = 1794189600.0
    assert orbit.sun_elevation([noon], HOME)[0] > 60
    assert orbit.sun_elevation([noon + 12 * 3600], HOME)[0] < -40


# ---- the catalogue (from tests/data/satellites: a few satellites cut from the SatNOGS DB) --------

DATA = Path(__file__).resolve().parent / "data" / "satellites"


def test_catalogue_from_satnogs():
    from sdr_toolkit.satellites import data
    cat = data.load(offline=True, folder=DATA)
    assert set(cat) == {25544, 33591, 57166, 27607, 43017}
    iss = cat[25544]
    assert iss.name == "ISS" and "Space stations" in iss.categories and "Fun to listen to" in iss.categories
    aprs = next(r for r in iss.radios if r.alive and r.downlink == 145825000)
    assert (aprs.kind, aprs.demod, aprs.decoder) == ("decode", "NFM", "aprs")
    sstv = next(r for r in iss.radios if r.alive and r.mode == "SSTV")
    assert (sstv.kind, sstv.demod, sstv.decoder) == ("decode", "NFM", "sstv")
    meteor = cat[57166]
    assert "Weather" in meteor.categories and any(r.kind == "pictures" and "SatDump" in r.how for r in meteor.radios)
    assert iss.radios[0].alive                                   # heard ones first
    assert all(r.kind == "out" for r in iss.radios if r.downlink > 1.8e9)


def test_downlinks_sorted_into_what_you_can_do():
    from sdr_toolkit.satellites.data import classify
    assert classify("FM", None, 145.8e6)[:2] == ("listen", "FM voice: listen in NFM")
    assert classify("FM", 9600, 437.5e6)[0] == "telemetry"
    assert classify("CW", None, 435.1e6)[4] == "morse"
    assert classify("USB", None, 145.9e6)[2] == "USB"
    assert classify("BPSK", 1200, 437.0e6)[0] == "telemetry"
    assert classify("FM", None, 2.4e9)[0] == "out" and classify("FM", None, 10e6)[0] == "out"
    assert classify("LRPT", 72000, 137.9e6)[0] == "pictures"


def test_telemetry_downlinks_go_to_gr_satellites():
    from sdr_toolkit.modes import sat_telemetry as T
    from sdr_toolkit.satellites.data import classify
    kind, how, demod, bw, decoder = classify("FSK", 9600, 437.2e6, "", norad=99998)
    assert (kind, decoder, demod) == ("telemetry", "sattlm", "NFM") and "plain AX.25" in how
    assert classify("AFSK", 1200, 437.0e6)[4] == "aprs"                    # plain 1200 baud AX.25: direwolf
    assert classify("AFSK", 4800, 437.0e6, norad=99998)[4] == ""           # not plain AX.25: needs its SatYAML
    assert classify("LoRa", None, 437.0e6)[4] == ""
    if T.known_norads():                                                    # gr-satellites is installed
        assert 22825 in T.known_norads()                                    # AO-27
        known = classify("AFSK", 1200, 436.795e6, norad=22825)
        assert known[4] == "sattlm" and "knows this satellite" in known[1]
    y = T.satyaml("TestSat", "FSK", 9600, 437.2e6)
    assert "framing: AX.25 G3RUH" in y and "baudrate: 9600" in y and "modulation: FSK" in y
    assert "af_carrier: 1700" in T.satyaml("T", "AFSK", 1200, 145.8e6)


def test_telemetry_packets_in_one_line():
    from sdr_toolkit.modes.sat_telemetry import describe
    ax25 = """Container: 
    header = Container: 
        addresses = ListContainer: 
            Container: 
                callsign = u'CQ' (total 2)
                ssid = Container: 
                    ch = True
                    ssid = 0
                    extension = False
            Container: 
                callsign = u'AO27' (total 4)
                ssid = Container: 
                    ch = True
                    ssid = 1
                    extension = True
        control = 0x03
        pid = 0xF0
    info = b'BAT 7.9V TEMP 21C' (total 17)""".splitlines()
    assert describe(ax25) == "From AO27-1 to CQ: BAT 7.9V TEMP 21C"
    fields = ["Container: ", "    batt_voltage = 7.92", "    temp_obc = 21", "    mode = u'nominal' (total 7)"]
    assert describe(fields) == "batt_voltage 7.92 · temp_obc 21 · mode nominal"


def test_right_click_picks_beacon_telemetry_and_repeater():
    from sdr_toolkit.satellites.data import Radio, Satellite, classify, pick

    def radio(desc, f, mode, baud=None, up=None, alive=True):
        kind, how, demod, bw, dec = classify(mode, baud, f, desc, norad=99998)
        return Radio(desc, f, f, up, mode, baud, alive, kind, how, demod, bw, dec, "Amateur")
    sat = Satellite(99998, "TestSat", radios=[
        radio("CW beacon", 435.1e6, "CW"), radio("9k6 telemetry", 437.2e6, "FSK", 9600),
        radio("FM repeater", 145.9e6, "FM", up=435.0e6), radio("old beacon", 435.2e6, "CW", alive=False)])
    assert pick(sat, "beacon").downlink == 435.1e6                        # heard ones first
    assert pick(sat, "telemetry").decoder == "sattlm"
    assert pick(sat, "repeater").downlink == 145.9e6
    assert pick(Satellite(1, "Quiet"), "beacon") is None


def test_satellite_mode_finds_the_next_passes():
    import time
    from sdr_toolkit.modes.satellites import SatelliteMode
    m = SatelliteMode()
    m.folder, m.offline = DATA, True
    m.start(dict(m.values, lat="-27.47", lon="153.03"))
    events = []
    end = time.time() + 30
    while time.time() < end and not any(e.kind == "sat_next" and e.data["done"] for e in events):
        time.sleep(0.1)
        events += m.poll()
    cat = next(e.data["catalogue"] for e in events if e.kind == "sat_catalogue")
    nxt = next(e.data["passes"] for e in events if e.kind == "sat_next" and e.data["done"])
    assert set(cat) == set(nxt)
    assert all(p.max_el >= 10 for ps in nxt.values() for p in ps)
    m.stop()
