"""Headless window check: every mode opens and runs; three decode a test signal.

    QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py [screenshot-folder]
"""

import faulthandler
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()      # its own logbook and aircraft database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
errors = []
faulthandler.dump_traceback_later(400, exit=True)          # a hang (a modal dialog) ends the test, with where


def hook(*a):
    errors.append(a)
    traceback.print_exception(*a)


sys.excepthook = hook

import numpy as np  # noqa: E402
from PyQt6.QtCore import QSettings  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

import signals  # noqa: E402
from sdr_toolkit import aircraftdb, appreports, gui  # noqa: E402
from sdr_toolkit.engines.morse.synth import transmission  # noqa: E402
from sdr_toolkit.engines.sstv.modes import BY_NAME  # noqa: E402
from sdr_toolkit.engines.sstv.synth import color_bars, encode  # noqa: E402

appreports.init = lambda *a, **k: None  # never send test failures as real reports
db_downloads = []
aircraftdb.download = lambda: db_downloads.append(1)  # no 9 MB download from the test
appreports.send_report = lambda *a, **k: None
appreports.report_error = lambda e, note=None: errors.append((type(e), e, None))

SIGNALS = {}


class FakeCapture:
    def __init__(self, target=None, rate=48000):
        self.rate = rate
        self.audio = SIGNALS.get(rate, np.zeros(0, np.float32))
        self.pos = 0

    def start(self):
        pass

    def read(self):
        n = self.rate // 10
        chunk = self.audio[self.pos : self.pos + n]
        self.pos += n
        if len(chunk) < n:
            chunk = np.concatenate([chunk, signals.noise(n - len(chunk), 0.001, self.pos)])
        return chunk

    def stop(self):
        pass


gui.AudioCapture = FakeCapture
ini = tempfile.mktemp(suffix=".ini")
gui.QSettings = lambda *a: QSettings(ini, QSettings.Format.IniFormat)

# Never open a real RTL-SDR: it may be plugged in and in use. If one is present, the
# RTL-SDR modes are pointed at an (absent) recording instead, so none of them starts.
DONGLE = any(i in __import__("subprocess").run(["lsusb"], capture_output=True, text=True).stdout
             for i in ("0bda:2838", "0bda:2832"))
if DONGLE:
    pre = QSettings(ini, QSettings.Format.IniFormat)
    for M in gui.MODES:
        if getattr(M, "source", "audio") == "radio":
            pre.setValue(f"{M.id}/input", "file")
    pre.sync()
shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None

app = QApplication([])
gui.MapView.online = False     # no tile downloads from tests
_real_init = gui.MapView.__init__
gui.MapView.__init__ = lambda self, *a, **k: _real_init(self, *a, **{**k, "online": False})
win = gui.MainWindow(Path(tempfile.mkdtemp()))
win.photos.online = False                               # no photo lookups from the test
win.resize(1280, 800)
win.show()


def pump(ticks):
    for _ in range(ticks):
        win.tick()
        app.processEvents()


# Every mode opens and runs: audio modes on quiet audio, RTL-SDR modes on their
# default input (no dongle here: they must say so, not hang or pop up).
from sdr_toolkit.modes.lora import lora_available  # noqa: E402

for M in gui.MODES:
    win.tree.setCurrentItem(win._items[M.id])
    app.processEvents()
    if DONGLE and getattr(M, "source", "audio") == "radio":
        assert not win.running and "recording" in win.title.text(), (M.id, win.title.text())
        continue
    if M.id == "lora" and lora_available() is None:
        assert not win.running and win.install_bar.isVisible() and "gr-lora_sdr" in win.install_label.text()
        continue
    if M.id == "receiver":
        # No dongle here: rtl_tcp finds none and the receiver says so instead of starting.
        assert not win.running and "No RTL-SDR" in win.title.text(), win.title.text()
        continue
    if M.id == "dmr":
        # No default frequency: it asks for one (or offers to install DSD-FME) instead of starting.
        assert not win.running and ("frequency" in win.title.text() or win.install_bar.isVisible())
        continue
    assert win.mode.id == M.id and win.running, M.id
    pump(20)
    if M.id in ("adsb", "ais", "ism"):
        # No dongle on the test machine: the window must say so (in a table mode too).
        import time as _t
        end = _t.time() + 8
        while _t.time() < end and "No RTL-SDR" not in (win.title.text() + win.text.toPlainText()):
            pump(1)
            _t.sleep(0.1)
        if "0bda:28" not in __import__("subprocess").run(["lsusb"], capture_output=True, text=True).stdout:
            assert "No RTL-SDR" in win.title.text() + win.text.toPlainText(), M.id
    if shots:
        win.grab().save(str(shots / f"mode_{M.id}.png"))

results = {}
# Morse: text output.
SIGNALS[8000] = np.concatenate([np.zeros(4000, np.float32), transmission("CQ TEST", 25, 20)])
win.tree.setCurrentItem(win._items["tones"])
win.tree.setCurrentItem(win._items["morse"])
pump(80)
results["morse"] = "CQ TEST" in win.text.toPlainText()
# A long message being decoded, a long status: the window keeps its width.
pump(5)
width, least = win.width(), win.minimumSizeHint().width()
long_text = "CQ CQ DE VK2ABC " * 400
win.handle([gui.Event("live", long_text), gui.Event("status", "status " + long_text)])
pump(5)
results["long_text_fits"] = (win.width() == width and win.minimumSizeHint().width() <= least
                             and win.live.text() == long_text and win.live.toolTip() == long_text
                             and QLabel.text(win.live).startswith("…") and QLabel.text(win.live).rstrip().endswith("VK2ABC"))
# Pager: table output.
SIGNALS[22050] = np.concatenate([np.zeros(10000, np.float32), signals.pocsag(1234567, "HELLO PAGER", 1200),
                                 np.zeros(10000, np.float32)])
win.tree.setCurrentItem(win._items["pager"])
pump(60)
win._stop()          # multimon-ng prints when its input ends
app.processEvents()
cells = [win.table.item(r, 5).text() for r in range(win.table.rowCount()) if win.table.item(r, 5)]
results["pager"] = "HELLO PAGER" in cells
if shots:
    win.grab().save(str(shots / "pager_decoded.png"))
# SSTV: picture output.
folder = tempfile.mkdtemp()
win.settings.setValue("sstv/folder", folder)
SIGNALS[12000] = np.concatenate([np.zeros(6000, np.float32), encode(color_bars(320, 240), BY_NAME["Robot 36"]),
                                 np.zeros(12000, np.float32)])
win.tree.setCurrentItem(win._items["sstv"])
pump(420)
results["sstv"] = "complete" in win.title.text() and len(list(Path(folder).glob("*.png"))) == 1
if shots:
    win.grab().save(str(shots / "sstv_decoded.png"))
# Click-to-tune on the waterfall changes the PSK frequency in the form.
win.tree.setCurrentItem(win._items["psk"])
win.on_waterfall_click(1234.0)
results["click"] = win.form.widgets["freq"][1].value() == 1234 and win.mode.dec.freq == 1234
# A mode whose decoder is missing offers an Install button (no commands), and installing
# runs deps.install and starts the mode again.
from sdr_toolkit import deps  # noqa: E402

real = deps.PARTS["readsb"]
deps.PARTS["readsb"] = ("readsb (ADS-B)", lambda: False, "system")
installed = []


class FakeInstall:
    def __init__(self, parent, parts, accept):
        installed.append((parts, accept))
        deps.PARTS["readsb"] = real
        self.still_missing = []

    def exec(self):
        return 0


gui.InstallDialog = FakeInstall
win.settings.setValue("adsb/input", "file")   # never the live dongle
win.tree.setCurrentItem(win._items["adsb"])
app.processEvents()
label = win.install_label.text()
results["offer"] = (win.install_bar.isVisible() and "readsb" in label and "sudo" not in label
                    and "apt" not in label and not win.running)
win.install_btn.click()
app.processEvents()
# After installing it starts again: here on an absent recording, so it asks for one.
results["install"] = (installed == [(["readsb"], False)] and not win.install_bar.isVisible()
                      and "recording" in win.title.text())
# Updates: a newer release shows the button; clicking it installs and restarts.
from sdr_toolkit import updater  # noqa: E402

fake = updater.Update("9.9.9", "https://api.github.com/repos/ssjrocks/sdr-toolkit/releases/assets/1",
                      "sdr_toolkit-9.9.9-py3-none-any.whl", "0" * 64)
calls = []
updater.check = lambda: fake
updater.install = lambda u: calls.append(("install", u.version))
updater.restart = lambda: calls.append(("restart",))
win.check_for_updates()
for _ in range(50):
    app.processEvents()
    __import__("time").sleep(0.02)
shown = win.update_btn.isVisible() and "9.9.9" in win.update_btn.text()
win.update_btn.click()
for _ in range(50):
    app.processEvents()
    __import__("time").sleep(0.02)
results["update"] = shown and calls == [("install", "9.9.9"), ("restart",)]
# The map shows only in map modes; the log windows collect lines while closed.
win.tree.setCurrentItem(win._items["morse"])
app.processEvents()
no_map = win.map.isHidden()
win.handle([gui.Event("log", "test decoded line"), gui.Event("raw", "test raw line")])
dl, rl = win.decoded_log, win.raw_log
win._show_log(dl)
app.processEvents()
shown_text = dl.view.toPlainText()
dl.pause.setChecked(True)
win.handle([gui.Event("log", "while paused")])
held = "while paused" not in dl.view.toPlainText() and "1 new" in dl.pause.text()
dl.pause.setChecked(False)
results["logs"] = (no_map and "test decoded line" in shown_text and "test raw line" in "\n".join(rl.lines)
                   and held and "while paused" in dl.view.toPlainText())
win.settings.setValue("ais/input", "file")       # never the live dongle
win.tree.setCurrentItem(win._items["ais"])
app.processEvents()
win.handle([gui.Event("points", data={"points": [{"id": 1, "lat": -33.85, "lon": 151.21, "kind": "ship",
                                                   "label": "TEST SHIP", "heading": 87,
                                                   "track": [(-33.86, 151.20), (-33.85, 151.21)]}],
                                      "home": (-33.87, 151.21)})])
app.processEvents()
results["map"] = win.map.isVisible() and len(win.map.points) == 1 and win.map.zoom >= 10
# ADS-B's frequency-correction button: the suggested value goes to the shared setting and the form.
win.tree.setCurrentItem(win._items["adsb"])
app.processEvents()
win.mode.values["ppm"] = 0
win.mode._ppm_readings = [57.2, 56.8]
win.mode.live = lambda: True
label = win.mode.actions()[0][1]
win.run_action("ppm")
del win.mode.live
results["ppm"] = ("+57 ppm" in label and int(win.settings.value("radio/ppm")) == 57
                  and win.form.widgets["ppm"][1].value() == 57 and win.mode.values["ppm"] == 57)
win.settings.setValue("radio/ppm", 0)
results["aircraft_db"] = len(db_downloads) >= 1        # ADS-B fetched the database by itself
# Details: picking a ship in the table shows it; the logbook has it and its positions.
win.tree.setCurrentItem(win._items["ais"])
app.processEvents()
win.logbook.clear()
ship = {"mmsi": 503123456, "name": "TEST SHIP", "type": "cargo", "callsign": "VJN1234", "imo": 9123456,
        "length": 150, "beam": 20, "lat": -33.85, "lon": 151.21, "speed": 12.3, "course": 87.0,
        "last": time.time(), "track": [(-33.86, 151.20), (-33.85, 151.21)]}
win.mode._ships = {503123456: ship}
win.mode._messages = 1
win.handle(win.mode._tables())
app.processEvents()
win.table.selectRow(0)
app.processEvents()
d = win.details
results["details"] = (win.side.currentWidget() is d and d.title.text() == "TEST SHIP"
                      and "9123456" in d.fields.text() and "150 × 20 m" in d.fields.text()
                      and "marinetraffic" in d.links.text() and win.map.selected == 503123456
                      and win.side.isTabVisible(win.side.indexOf(d)))
win.logbook.flush()
crafted = win.logbook.craft("ais")
results["logbook"] = bool(crafted and crafted[0]["key"] == "503123456" and crafted[0]["name"] == "TEST SHIP"
                      and crafted[0]["info"]["imo"] == 9123456 and win.logbook.positions("ais", 0, 1e10))
win.show_logbook()
app.processEvents()
lw = win._logbook_window
results["logbook_window"] = lw.spotted.rowCount() == 1 and lw.spotted.item(0, 2).text() == "TEST SHIP"
lw.close()
# The ship leaves: "All tracks" keeps it on the map where it was last heard, and it can still be picked.
win.handle([gui.Event("points", data={"points": [], "home": None})])
win.map.tracks_btn.setChecked(True)
app.processEvents()
spot = win.map._screen(-33.85, 151.21)
results["session_tracks"] = (503123456 in win.map.session and len(win.map.session[503123456]["track"]) == 2
                             and win.map.point_at(spot) == 503123456)
win.map.tracks_btn.setChecked(False)
win.mode._ships = {}
win.refresh_details()
results["details_gone"] = "not heard now" in d.subtitle.text() and d.title.text() == "TEST SHIP"
# Replay from the logbook.
from sdr_toolkit.panels import ReplayWindow  # noqa: E402
rw = ReplayWindow(win, win.logbook, "ais", online=False)
rng = win.logbook.time_range("ais")
results["replay"] = (bool(rw.tracks) and len(rw.points_at(rng[1])) == 1
                     and rw.points_at(rng[1] + 3600) == [])
rw.close()
# ADS-B range rings from aircraft flying out to 100 km north.
from sdr_toolkit import coverage  # noqa: E402
win.tree.setCurrentItem(win._items["adsb"])
app.processEvents()
here = (-33.87, 151.21)
win.coverage = coverage.Coverage(here)
for km in range(5, 101):
    lat, lon = coverage.destination(here, 0.0, km)
    win.coverage.add("7c1234", km * 2.0, lat, lon)
win._coverage_drawn = 0
win.handle([gui.Event("points", data={"points": [], "home": here})])
results["rings"] = (win.map.rings is not None and win.map.rings_btn.isVisible() is not None
                    and not win.map.rings_btn.isHidden() and "furthest 100 km" in win.map.rings_btn.toolTip())
# The estimated range: aircraft between north and east, positions every second to 70 km, patchy to
# 100 km, then two minutes of other messages (450 kt: 28 km more).
for n, brg in enumerate(range(5, 95, 10)):
    key, t, km = f"est{n}", 10000.0 * (n + 1), 1.0
    win.coverage.heard(key, t)
    while km < 100:
        t += 1
        km += 450 * 1.852 / 3600
        win.coverage.heard(key, t)
        if km < 70 or int(t) % 20 == 0:
            lat, lon = coverage.destination(here, brg, km)
            win.coverage.add(key, t, lat, lon, 450, brg)
    for i in range(1, 121):
        win.coverage.heard(key, t + i)
win._coverage_drawn = 0
win.handle([gui.Event("points", data={"points": [], "home": here})])
mx, rel, est = win.map.rings
results["rings_estimated"] = (est is not None and "heard to about" in win.map.rings_btn.toolTip()
                              and 125 < est[4][1] < 131 and 95 < mx[4][1] < 101 and 60 < rel[4][1] <= 70)
if shots:
    win.map.refit()
    win.map.grab().save(str(shots / "rings.png"))
win._save_coverage()
results["rings_saved"] = bool(win.logbook.load_coverage("adsb", coverage.site_key(here)))
# Connect relay: by address to a relay playing a recording, then disconnect.
relay_src = Path(__file__).resolve().parents[2] / "sdr-tools-relay" / "src"
if relay_src.is_dir():
    sys.path.append(str(relay_src))
    import socket as _socket  # noqa: E402
    from sdr_relay.server import Config as _RC, Relay as _Relay  # noqa: E402
    _s = _socket.socket()
    _s.bind(("127.0.0.1", 0))
    _rport = _s.getsockname()[1]
    _s.close()
    _rec = Path(tempfile.mkdtemp()) / "r.cu8"
    _rec.write_bytes(bytes(512))
    _relay = _Relay(_RC(name="Garage relay", port=_rport, password="pw", file=str(_rec)), version="t")
    _relay.start()
    from sdr_toolkit import relay as _relay_mod  # noqa: E402
    from sdr_toolkit.panels import RelayDialog  # noqa: E402
    _relay_mod.latest_relay_version = lambda: "0.1.2"       # no GitHub lookup from the test
    dlg = RelayDialog(win, win.settings)
    dlg.tabs.setCurrentIndex(2)
    dlg.address.setText(f"127.0.0.1:{_rport}")
    dlg.password.setText("wrong")
    dlg.connect_selected()
    for _ in range(50):
        app.processEvents()
        time.sleep(0.05)
    refused = "wrong password" in dlg.note.text()
    dlg.password.setText("pw")
    dlg.connect_selected()
    for _ in range(50):
        app.processEvents()
        time.sleep(0.05)
        if dlg.bridge:
            break
    win._relay_changed("connected")
    results["relay"] = refused and dlg.bridge is not None and win.relay_btn.text() == "Relay: Garage relay ▾"
    _relay.on_update = lambda who: (True, "9.9.9")
    dlg.bridge.features = ["check", "update"]
    dlg.bridge.relay_version = "0.1.2"
    win._relay_latest("0.2.0")                 # what GitHub would say
    act = win._relay_update_action
    results["relay_update_offer"] = act.isEnabled() and "Update the relay to 0.2.0" in act.text()
    win._relay_latest("0.1.2")
    results["relay_update_offer"] = results["relay_update_offer"] and not act.isEnabled() and "latest" in act.text()
    # Range rings over a relay on this network: its own, apart from this PC's dongle's.
    win.tree.setCurrentItem(win._items["adsb"])
    app.processEvents()
    win.mode.values.update(input="rtl", lat="-33.87", lon="151.21")      # nothing is started
    win._setup_coverage()
    results["relay_rings"] = (win.coverage is not None and win._coverage_site.endswith(" relay:Garage relay")
                              and "relay Garage relay" in win._coverage_source)
    win.coverage = None
    win.mode.values["input"] = "file"
    win.disconnect_relay()
    results["relay"] = results["relay"] and win.relay_btn.text() == "Connect relay…" and win.relay_btn.menu() is None
    dlg.close()
    _relay.stop()
# Spectrum & receiver on a recording: frames reach the waterfall, clicking tunes, a mode brings its bandwidth.
import test_receiver as _TR  # noqa: E402
from sdr_toolkit.modes import receiver as _R  # noqa: E402

_R.AudioOut = _TR.FakeAudio                      # no sound from the test
_fm = _TR.fm_station(Path(tempfile.mkdtemp()) / "fm.cu8", seconds=1.0)
win.settings.setValue("receiver/input", "file")  # never the live dongle
win.settings.setValue("receiver/file", str(_fm))
win.settings.setValue("receiver/freq_mhz", 101.1)
win.settings.setValue("receiver/decoder", "tones")  # chosen last time: it must run from the start
win.tree.setCurrentItem(win._items["receiver"])
for _ in range(40):
    app.processEvents()
    time.sleep(0.05)
rp = win.receiver_panel
results["saved_decoder_runs"] = win.mode.decoder is not None and win.mode.decoder.id == "tones"
results["receiver"] = (win.output.currentWidget() is rp and rp.view.db is not None and rp.view.center == 100.5e6
                       and "Signal" in rp.meter.text())
rp.view.tuned.emit(101.2e6 + 30)                 # a click: rounded to the 100 kHz step
app.processEvents()
results["receiver_tune"] = (abs(rp.freq.value() - 101.2) < 1e-9 and win.mode.values["freq_mhz"] == 101.2
                            and win.form.widgets["freq_mhz"][1].value() == 101.2 and win.mode.center == 100.5e6)
rp.mode.setCurrentIndex(rp.mode.findData("NFM"))
app.processEvents()
results["receiver_mode"] = (win.mode.demod.mode == "NFM" and win.mode.values["bandwidth"] == 12500
                            and rp.bw.value() == 12500)
# Identify, decode, tag, slide and survey on the recording.
win.mode.identify(auto=True)                       # a click would open the guesses menu (modal)
for _ in range(40):
    app.processEvents()
    time.sleep(0.05)
    if win._guesses:
        break
results["identify"] = bool(win._guesses) and "FM broadcast" in rp.looks.text()
win._receiver_changed("decoder", "tones")
for _ in range(20):
    app.processEvents()
    time.sleep(0.05)
rp.show_values(dict(win.mode.values, decoder="dmr"))
results["voice_slider"] = rp.voice.isVisible() and rp.voice.value() == 100
rp.show_values(win.mode.values)
results["voice_slider"] = results["voice_slider"] and not rp.voice.isVisible()
pump(5)                                             # let the layout settle first
width, least = win.width(), win.minimumSizeHint().width()
win.decode_area.handle(gui.Event("live", "RYRYRY " * 600))
rp.looks.setText("Looks like: " + "a very long guess " * 80)
pump(5)
results["decode_long_text_fits"] = win.width() == width and win.minimumSizeHint().width() <= least
# Identify's progress on its button, and back when stopped.
ib = rp.identify_btn
win.handle([gui.Event("identifying", data={"stage": "listening", "left": 5.2, "freq": 101.1e6})])
listening = ib.text() == "Listening… 5 s" and not ib.isEnabled()
win.handle([gui.Event("identifying", data={"stage": "trying", "done": 2, "total": 4, "freq": 101.1e6})])
trying = ib.text() == "Decoding… 2/4"
win.handle([gui.Event("identifying", data={"stage": "cancelled", "freq": 101.1e6})])
results["identify_progress"] = listening and trying and ib.text() == "Identify" and ib.isEnabled()
results["decode_area"] = (win.decode_area.isVisible() and win.decode_area.M.id == "tones"
                          and win.mode.decoder is not None and win.mode.values["demod"] == "NFM")
win.logbook.add_tag(101.3e6, "Test station", "WFM", 180000, "")
win._refresh_tags()
results["tags"] = [t["label"] for t in rp.view.tags] == ["Test station"]
win._listen_tag(rp.view.tags[0])
results["tags"] = results["tags"] and win.mode.values["freq_mhz"] == 101.3 and win.mode.values["demod"] == "WFM"
win._receiver_slid(100.2e6)
results["slide"] = win.mode.center == 100.2e6
# Decode points: a channel decoded as well, on the spectrum and in the decode area, saved and removed.
import json as _json  # noqa: E402
pf = rp.view.center + 300000
win.add_point(pf, "tones")
for _ in range(20):
    time.sleep(0.05)
    pump(1)
results["decode_point"] = (len(win.mode.points) == 1 and [p_["freq"] for p_ in rp.view.points] == [pf]
                           and win.decode_area.points_box.isVisible() and "Decoding 1 channel" in
                           win.decode_area.points_title.text()
                           and _json.loads(win.settings.value("receiver/points"))[0]["decoder"] == "tones")
if shots:
    win.grab().save(str(shots / "decode_points.png"))
win.remove_all_points()
for _ in range(10):
    time.sleep(0.05)
    pump(1)
results["decode_point_removed"] = (not win.mode.points and not rp.view.points
                                   and not win.decode_area.points_box.isVisible() and not win._saved_points())
# The step buttons: 80% of what's shown; the waterfall's history moves with the band, not wiped.
view = rp.view
before = view.center
view.step_btns[1].click()
pump(10)
moved = win.mode.center - before
results["step_button"] = abs(moved - 0.8 * view.rate) < 1 and view.step_btns[0].isVisible()
# The waterfall's history moves with the band: a signal 600 kHz up is 256 bins further left afterwards.
from sdr_toolkit.receiver.view import SpectrumView  # noqa: E402
sv_ = SpectrumView()
sv_.resize(800, 400)
line = np.full(1024, -100.0)
line[600] = -20
for _ in range(3):
    sv_.set_frame(line, 100e6, 2.4e6)
sv_.set_frame(np.full(1024, -100.0), 100.6e6, 2.4e6)
row = sv_.rows()[1]
black = sv_.cmap[0]
results["waterfall_kept"] = (list(np.flatnonzero(row[:768] != row[0])) == [600 - 256]
                             and (row[-256:] == black).all())
for _ in range(30):                                  # the dongle's frames from its new centre
    time.sleep(0.1)
    pump(2)
    if view._slide_center is None:
        break
results["step_arrived"] = view._slide_center is None and abs(view.center - win.mode.center) < 1
if shots:
    win.grab().save(str(shots / "stepped.png"))
view.step_btns[0].click()
pump(10)
results["step_button"] = results["step_button"] and abs(win.mode.center - before) < 1
rp.survey_btn.setChecked(True)
results["dwell_shown"] = rp.dwell.isVisible()
rp.dwell.setValue(5)                                   # 50 ms
for _ in range(40):
    app.processEvents()
    time.sleep(0.05)
results["survey"] = (rp.pages.currentWidget() is rp.survey_view and rp.survey_view.db is not None
                     and win.mode.survey is not None)
# Zooming keeps what was captured and doesn't change the sweep; the button does.
sv = rp.survey_view
cap = sv.cap
sweep = (win.mode.survey.lo, win.mode.survey.hi)
sv.lo, sv.hi = 99e6, 101e6
sv._clamp(2e6)
sv.repaint()
results["survey_zoom"] = sv.cap is cap and sv._wf is not None and sv._wf.count > 0 and (
    win.mode.survey.lo, win.mode.survey.hi) == sweep
rp.sweep_shown_btn.click()
for _ in range(20):
    app.processEvents()
    time.sleep(0.05)
results["survey_sweep_shown"] = (win.mode.survey.lo, win.mode.survey.hi) == (99e6, 101e6) and sv.cap is cap
results["dwell"] = (win.mode.values["dwell_ms"] == 50 and win.mode.survey.dwell == 0.05
                   and rp.dwell_label.text() == "Dwell 50 ms")
if shots:
    win.grab().save(str(shots / "survey.png"))
rp.survey_view.listen.emit(98.5e6)
for _ in range(10):
    app.processEvents()
    time.sleep(0.05)
results["survey_listen"] = (not rp.survey_btn.isChecked() and rp.pages.currentWidget() is rp.view
                            and win.mode.values["freq_mhz"] == 98.5 and win.mode.survey is None)
win._receiver_changed("decoder", "")
if shots:
    win.grab().save(str(shots / "receiver.png"))
win._stop()
if shots:
    win.grab().save(str(shots / "ais_map.png"))
    dl.grab().save(str(shots / "decoded_log.png"))
win.close()
print(results, "errors:", len(errors))
ok = all(results.values()) and not errors
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
