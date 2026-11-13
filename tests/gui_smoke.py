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

REAL_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sdr-toolkit"
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
from sdr_toolkit.modes import digital_voice as _dv, lora as _lora  # noqa: E402

# Parts built for the app (gr-lora_sdr, DSD-FME) are where they really are, not in the test's data folder.
_lora.LORA_PREFIX = REAL_DATA / "gr-lora_sdr"
_dv.DSD_FME = REAL_DATA / "dsd-fme" / "bin" / "dsd-fme"
# The radiosonde decoders: the app's install or this checkout's build (found on PATH).
for _d in (REAL_DATA / "radiosonde" / "bin", Path(__file__).resolve().parents[1] / "build/ext/RS/demod/mod",
           Path(__file__).resolve().parents[1] / "build/ext/RS/scan"):
    if _d.is_dir():
        os.environ["PATH"] = f"{_d}:{os.environ['PATH']}"
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
from sdr_toolkit.modes.satellites import SatelliteMode  # noqa: E402
SatelliteMode.folder = Path(__file__).resolve().parent / "data" / "satellites"   # no downloads either
SatelliteMode.offline = True
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
    if getattr(M, "hidden", False):
        continue                                  # not in the list (the one-frequency DMR decoder)
    win.tree.setCurrentItem(win._items[M.id])
    app.processEvents()
    # Not installed comes first: it's said before anything is read (a dongle plugged in or not).
    if M.id == "sondes" and gui.deps.missing(["radiosonde"]):
        assert not win.running and win.install_bar.isVisible() and "radiosonde" in win.install_label.text()
        continue
    if M.id == "lora" and lora_available() is None:
        assert not win.running and win.install_bar.isVisible() and "gr-lora_sdr" in win.install_label.text()
        continue
    if DONGLE and getattr(M, "source", "audio") == "radio":
        # A dongle is plugged in: the mode mustn't touch it. It asks for a recording, or offers to
        # install what it's missing (parts are looked for in this test's own data folder).
        assert not win.running and ("recording" in win.title.text() or win.install_bar.isVisible()), (
            M.id, win.title.text())
        continue
    if M.id in ("receiver", "explorer"):
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
results["logbook_window"] = (lw.spotted.rowCount() == 1 and lw.spotted.item(0, 2).text() == "TEST SHIP"
                             and lw.spotted.isSortingEnabled() and lw.received.isSortingEnabled())
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
# Markers move on between reports; one whose positions stopped 20 s ago is a flashing prediction.
now = time.time()
fresh = coverage.destination(here, 200.0, 30.0)
stale = coverage.destination(here, 30.0, 60.0)
win.map.show_rings = False
win.handle([gui.Event("points", data={"home": here, "heard": {}, "points": [
    {"id": "aa0001", "kind": "aircraft", "lat": fresh[0], "lon": fresh[1], "pos_t": now - 0.5, "msg_t": now - 0.5,
     "gs": 300, "course": 200.0, "heading": 200.0, "label": "FRESH 12000", "track": []},
    {"id": "aa0002", "kind": "aircraft", "lat": stale[0], "lon": stale[1], "pos_t": now - 20, "msg_t": now - 1,
     "gs": 450, "course": 30.0, "heading": 30.0, "label": "STALE 35000", "track": []}]})])
win.map.refit()
live = {p_["id"]: win.map.live(p_) for p_ in win.map.current()}
results["map_dead_reckoning"] = (win.map._anim.isActive() and not live["aa0001"]["predicted"]
                                 and live["aa0002"]["predicted"] and live["aa0002"]["lat"] > stale[0])
if shots:
    win.map.grab().save(str(shots / "predicted.png"))
win.map.show_rings = True
win.handle([gui.Event("points", data={"points": [], "home": here})])
win._save_coverage()
results["rings_saved"] = bool(win.logbook.load_coverage("adsb", coverage.site_key(here)))
# Tables sort by a clicked column, numbers as numbers, and stay sorted (and selected) as they refresh.
from PyQt6.QtCore import Qt as _Qt  # noqa: E402
t = win.table
cols = [k for k, _ in win.mode.columns]
seen_col = cols.index("seen")
win._set_rows([{"hex": "AA0001", "seen": "10 s"}, {"hex": "AA0002", "seen": "9 s"}, {"hex": "AA0003", "seen": "120 s"}])
before = [t.item(r, 0).text() for r in range(3)]
t.sortByColumn(seen_col, _Qt.SortOrder.AscendingOrder)  # what a click on its heading does
win._selected = "aa0001"
win._set_rows([{"hex": "AA0001", "seen": "11 s"}, {"hex": "AA0002", "seen": "1 s"}, {"hex": "AA0003", "seen": "121 s"}])
after = [t.item(r, 0).text() for r in range(3)]
results["table_sorting"] = (before == ["AA0001", "AA0002", "AA0003"] and after == ["AA0002", "AA0001", "AA0003"]
                            and t.selectionModel().isRowSelected(1) and t.isSortingEnabled())
win._selected = None
t.sortByColumn(-1, _Qt.SortOrder.AscendingOrder)
# Tables stay where the user scrolled them as data comes; only a pick elsewhere (the map) moves them.
from PyQt6.QtWidgets import QHeaderView as _QHV  # noqa: E402
_rows = [{"hex": f"AB{i:04d}", "seen": f"{i} s"} for i in range(300)]
win._set_rows(_rows)
app.processEvents()
_bar = t.verticalScrollBar()
_bar.setValue(_bar.maximum() // 2)
_v0 = _bar.value()
win._selected = "ab0003"                               # selected, far above what's shown
win._set_rows(list(reversed(_rows)) + [{"hex": "AB9999", "seen": "1 s"}])
app.processEvents()
_r3 = next(r for r in range(t.rowCount()) if t.item(r, 0).text() == "AB0003")
results["table_stays"] = _v0 > 0 and _bar.value() == _v0 and t.selectionModel().isRowSelected(_r3)
win.map.picked.emit("ab0003")                          # a click on its plane on the map
app.processEvents()
results["table_map_jump"] = (_bar.value() != _v0
                             and t.visualItemRect(t.item(_r3, 0)).intersects(t.viewport().rect()))
_hh = t.horizontalHeader()
results["table_columns"] = _hh.sectionsMovable() and _hh.sectionResizeMode(0) == _QHV.ResizeMode.Interactive
_seen_c = cols.index("seen")
_hh.resizeSection(_seen_c, 41)                         # as a drag on its edge does
win._set_rows([{"hex": "AB0001", "seen": "a much longer value than the column has room for"}])
app.processEvents()
results["table_columns"] = results["table_columns"] and _hh.sectionSize(_seen_c) == 41
win._selected = None
win._set_rows([])
# The user's column order is kept per mode, through other modes' tables (each its own layout).
_hh.moveSection(_hh.visualIndex(0), 2)
_was_running = win.running
win._stop()                                            # only the tables here: not ADS-B started again
for _m in ("ais", "adsb"):
    win.tree.setCurrentItem(win._items[_m])
    app.processEvents()
    if _m == "ais":
        _ais = [_hh.sectionSize(i) for i in range(t.columnCount())]
        _ais_ok = all(x >= 30 for x in _ais) and _hh.visualIndex(0) == 0
results["table_layout_kept"] = (_ais_ok and win.mode.id == "adsb" and _hh.visualIndex(0) == 2
                                and _hh.sectionSize(_seen_c) == 41
                                and all(_hh.sectionSize(i) >= 30 or i == _seen_c for i in range(t.columnCount())))
from sdr_toolkit.widgets import ORDER, SortItem, drop_oldest, filling, sortable  # noqa: E402
from PyQt6.QtWidgets import QTableWidget  # noqa: E402
lt = sortable(QTableWidget(0, 1))
for i, text in enumerate(["b", "c", "a"]):
    with filling(lt):
        lt.insertRow(lt.rowCount())
        it = SortItem(text)
        it.setData(ORDER, i)
        lt.setItem(lt.rowCount() - 1, 0, it)
lt.sortByColumn(0, _Qt.SortOrder.AscendingOrder)
drop_oldest(lt)
results["table_drop_oldest"] = [lt.item(r, 0).text() for r in range(lt.rowCount())] == ["a", "c"]
# Satellite tracker: the list, a satellite's passes and radios, where it is, and listening to one
# (Spectrum & receiver tuned there, following its Doppler shift).
win.settings.setValue("radio/lat", "-27.47")
win.settings.setValue("radio/lon", "153.03")
win.tree.setCurrentItem(win._items["satellites"])
sp = win.satellite_panel
for _ in range(100):
    time.sleep(0.1)
    pump(1)
    if sp.catalogue and len(sp.next_passes) == len(sp.catalogue):
        break
names = {sp.list.item(r, 0).text() for r in range(sp.list.rowCount())}
results["sat_list"] = {"ISS", "FOX-1B"} <= names and "METEOR M2-3" not in names      # weather isn't "fun"
sp.category.setCurrentIndex(sp.category.findData(""))
results["sat_list"] = results["sat_list"] and sp.list.rowCount() == 5 and sp.coming.rowCount() > 0
sp.select(25544)
sp.tick()
pt = win.satellite_panel.map.points
results["sat_detail"] = (sp.pass_table.rowCount() > 5 and sp.radio_table.rowCount() > 20 and sp.sky.path is not None
                         and pt and pt[0]["kind"] == "satellite" and len(pt[0]["past_xy"]) > 700
                         and len(pt[0]["ahead_xy"]) > 700 and len(pt[0]["ticks_xy"]) == 12
                         and sp.map.footprints and "ISS" in sp.now_label.text())
sp.best.setChecked(True)
highs = [float(sp.pass_table.item(r, 2).text().split("°")[0]) for r in range(sp.pass_table.rowCount())]
results["sat_best_passes"] = highs == sorted(highs, reverse=True)
# A satellite clicked on the map is picked in the list too, its tabs filled (the Radio tab stays
# open); a click on empty map lets it go and shows every one that's up again.
sp.tabs.setCurrentIndex(2)
sp.map.picked.emit("sat27607")
app.processEvents()
_lr = sp.list.selectionModel().selectedRows()
results["sat_map_pick"] = (sp.selected is not None and sp.selected.norad == 27607 and len(_lr) == 1
                           and sp.list.item(_lr[0].row(), 0).data(_Qt.ItemDataRole.UserRole) == 27607
                           and sp.radio_table.rowCount() == len(sp.selected.radios) > 0
                           and sp.tabs.currentIndex() == 2)
_cleared = []
sp.map.cleared.connect(lambda: _cleared.append(1))
from PyQt6.QtCore import QPoint as _QPoint  # noqa: E402
from PyQt6.QtTest import QTest as _QTest  # noqa: E402
_cat0 = sp.category.currentIndex()
sp.category.setCurrentIndex(sp.category.findData("has:FM repeaters (ham)"))   # a list narrower than all
_QTest.mouseClick(sp.map, _Qt.MouseButton.LeftButton, pos=_QPoint(3, sp.map.height() - 3))   # away from markers
app.processEvents()
results["sat_map_clear"] = (bool(_cleared) and sp.selected is None and not sp.list.selectionModel().selectedRows()
                            and sp.radio_table.rowCount() == 0 and not sp.map.footprints
                            and {p_["id"] for p_ in sp.map.points} == {f"sat{s_.norad}" for s_ in sp.shown()}
                            and 0 < len(sp.shown()) < len(sp.catalogue))    # the list as filtered, not all
# Each one in the list gets its ±12 h ground track (worked out in the background) and hourly dots.
for _ in range(50):
    sp.tick()
    app.processEvents()
    if sp.map.points and all("ahead_xy" in p_ for p_ in sp.map.points):
        break
    time.sleep(0.1)
sp.map.refit()
app.processEvents()
_in_view = all(0 <= sp.map._screen(p_["lat"], p_["lon"]).x() <= sp.map.width()
               and 0 <= sp.map._screen(p_["lat"], p_["lon"]).y() <= sp.map.height() for p_ in sp.map.points)
results["sat_list_tracks"] = _in_view and (len(sp.map.points) >= 2 and all(
    len(p_.get("past_xy", ())) > 700 and 700 < len(p_.get("ahead_xy", ())) <= 721 and len(p_.get("ticks_xy", ())) == 12
    and p_.get("hour_labels") for p_ in sp.map.points)) and sp.category.itemText(
        sp.category.findData("has:FM repeaters (ham)")) == "With FM repeaters (ham)"
if shots:
    sp.map.refit()
    app.processEvents()
    win.grab().save(str(shots / "satellite_tracks.png"))
sp.category.setCurrentIndex(_cat0)
sp.select(25544)
sp.tabs.setCurrentIndex(0)
# The list by what satellites carry: every one with an FM (ham) repeater, every one sending SSTV.
_cat = sp.category.currentIndex()
sp.category.setCurrentIndex(sp.category.findData("has:FM repeaters (ham)"))
_reps = {sp.list.item(r, 0).text() for r in range(sp.list.rowCount())}
sp.category.setCurrentIndex(sp.category.findData("has:SSTV pictures"))
_sstv = {sp.list.item(r, 0).text() for r in range(sp.list.rowCount())}
results["sat_filters"] = {"ISS", "SAUDISAT 1C"} <= _reps and _sstv == {"ISS"}
sp.category.setCurrentIndex(_cat)
if shots:
    win.grab().save(str(shots / "satellites.png"))
    sp.tabs.setCurrentIndex(2)
    app.processEvents()
    win.grab().save(str(shots / "satellite_radio.png"))
    sp.tabs.setCurrentIndex(3)
    app.processEvents()
    win.grab().save(str(shots / "satellite_sky.png"))
aprs_row = next(r for r in range(sp.radio_table.rowCount())
                if sp.selected.radios[sp.radio_table.item(r, 0).data(_Qt.ItemDataRole.UserRole)].downlink == 145825000
                and sp.selected.radios[sp.radio_table.item(r, 0).data(_Qt.ItemDataRole.UserRole)].alive)
sp.radio_table.selectRow(aprs_row)
sp._listen()
pump(5)
f = float(win.mode.values.get("freq_mhz", 0))
results["sat_listen"] = (win.mode.id == "receiver" and abs(f - 145.825) < 0.004 and win.mode.values["decoder"] == "aprs"
                         and win.mode.values["demod"] == "NFM" and win._doppler_timer.isActive())
win._receiver_changed("freq_mhz", 101.1)
win._doppler_step()
results["sat_listen"] = results["sat_listen"] and not win._doppler_timer.isActive()
# Right-click a satellite: listen for its beacon, telemetry or repeater. Telemetry goes to gr-satellites
# (the Satellite telemetry decoder), told which satellite.
win.tree.setCurrentItem(win._items["satellites"])
pump(5)
from PyQt6.QtWidgets import QMenu as _QMenu  # noqa: E402
_menus = []
_orig_exec = _QMenu.exec
_QMenu.exec = lambda self, *a: _menus.append([(x.text(), x.isEnabled(), x) for x in self.actions()])
_row = next(r for r in range(sp.list.rowCount()) if sp.list.item(r, 0).data(_Qt.ItemDataRole.UserRole) == 25544)
sp._menu(sp.list, sp.list.visualItemRect(sp.list.item(_row, 0)).center())
_QMenu.exec = _orig_exec
_acts = _menus[0] if _menus else []
_texts = [t for t, _, _ in _acts]
results["sat_menu"] = (len(_acts) == 4 and _texts[0] == "ISS" and "beacon" in _texts[1] and "telemetry" in _texts[2]
                       and "437.800 FM voice" in _texts[3] and all(e for _, e, _ in _acts[1:]))
if _acts:
    _acts[2][2].trigger()
    pump(10)
    from sdr_toolkit.modes import MODES as _MODES  # noqa: E402
    _TM = next(m for m in _MODES if m.id == "sattlm")
    results["sat_telemetry"] = (win.mode.id == "receiver" and win.mode.values["decoder"] == "sattlm"
                                and str(win.settings.value(win._key(_TM, "satellite"))) == "25544"
                                and win._doppler_timer.isActive())
    win._receiver_changed("decoder", "")
    win._receiver_changed("bandwidth", 12500)          # as the APRS listen above leaves it
    win._receiver_changed("freq_mhz", 101.1)
    win._doppler_step()
win._receiver_changed("decoder", "")
# Radiosondes on a recording of the real RS41: found, decoded, in the table and on the map.
if not gui.deps.missing(["radiosonde"]):
    import test_modes as _TM  # noqa: E402
    _rec = _TM.sonde_iq(Path(tempfile.mkdtemp()) / "sonde.cu8", (402.050e6,))
    win.settings.setValue("sondes/input", "file")
    win.settings.setValue("sondes/file", str(_rec))
    win.settings.setValue("sondes/file_rate", 240000)
    win.tree.setCurrentItem(win._items["sondes"])
    for _ in range(150):
        time.sleep(0.1)
        pump(1)
        if win.table.rowCount() and win.map.points:
            break
    cells = [win.table.item(0, c).text() for c in range(win.table.columnCount())] if win.table.rowCount() else []
    results["sondes"] = (win.mode.id == "sondes" and "K1930308" in cells
                         and any(p_["kind"] == "sonde" for p_ in win.map.points))
    if shots:
        win.map.refit()
        app.processEvents()
        win.grab().save(str(shots / "sondes.png"))
# The DMR / P25 explorer: its lists show under the spectrum; a call fills the talkgroup and radio lists.
win.tree.setCurrentItem(win._items["explorer"])
for _ in range(10):
    time.sleep(0.05)
    pump(1)
from sdr_toolkit.modes.base import Event as _Ev  # noqa: E402
from sdr_toolkit.receiver import dsd as _dsd  # noqa: E402
if win.explorer is None:
    win._explorer_start()
for _line in ("Sync: +DMR  [SLOT1]  slot2  | Color Code=01 | VLC", " SLOT 1 TGT=505 SRC=3125678 Group Call"):
    win._explorer_event(438.8e6, _Ev("dsd", data=_dsd.parse(_line)))
win._ex_timer.timeout.emit()
xp = win.explorer_pane
results["explorer"] = (xp.isVisible() and xp.talkgroups.rowCount() >= 1 and xp.radios.rowCount() >= 1
                       and "digital voice channel" in xp.title.text() and xp.channels.rowCount() >= 1
                       and not win.decode_area.points_box.isVisible())
if shots:
    win.grab().save(str(shots / "explorer.png"))
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
# Typing a frequency isn't interrupted: nothing writes into the box until Enter (or leaving it).
from PyQt6.QtTest import QTest as _QT  # noqa: E402
import traceback as _tb  # noqa: E402
_writes = []
_orig_set = rp.freq.setValue
rp.freq.setValue = lambda v: (_writes.append("".join(_tb.format_stack(limit=6))), _orig_set(v))
_le = rp.freq.lineEdit()
rp.freq.setFocus()
_le.selectAll()
_QT.keyClicks(_le, "10")
for _ in range(10):
    pump(1)
    time.sleep(0.05)
win._receiver_changed("freq_mhz", 101.13)            # as Doppler following retunes, every second
for _ in range(10):
    pump(1)
    time.sleep(0.05)
_typed = _le.text()
rp.freq.setValue = _orig_set
results["freq_typing"] = _typed.startswith("10") and not _writes
_le.setText(f"{101.2:.6f}")
_QT.keyClick(_le, _Qt.Key.Key_Return)
app.processEvents()
# The Lock button turns decode points' following of their signal off and on.
rp.lock_btn.setChecked(False)
app.processEvents()
_unlocked = win.mode.values.get("afc") is False and not win.form.widgets["afc"][1].isChecked()
rp.lock_btn.setChecked(True)
app.processEvents()
results["lock_button"] = _unlocked and win.mode.values.get("afc") is True
# A settings dropdown opens at its box's width (a wide one ran off the window onto the next monitor).
_dm = win.form.widgets["demod"][1]
_dm.showPopup()
app.processEvents()
results["dropdown_in_box"] = _dm.view().window().width() <= _dm.width() + 2 and _dm.view().window().isVisible()
_dm.hidePopup()
rp.view.tuned.emit(101.2e6 + 30)                 # a click: to the nearest kHz
app.processEvents()
results["receiver_tune"] = (abs(rp.freq.value() - 101.2) < 1e-9 and win.mode.values["freq_mhz"] == 101.2
                            and win.form.widgets["freq_mhz"][1].value() == 101.2 and win.mode.center == 100.5e6)
rp.view.tuned.emit(101.1372e6)                   # not to the 100 kHz tuning step: where it was clicked
app.processEvents()
results["receiver_tune"] = results["receiver_tune"] and win.mode.values["freq_mhz"] == 101.137
# A click on the grab bar that twitches a pixel or two doesn't slide the band.
from PyQt6.QtCore import QPointF as _P  # noqa: E402
from PyQt6.QtGui import QMouseEvent as _ME  # noqa: E402
_slides = []
rp.view.slid.connect(_slides.append)
_B, _NB = _Qt.MouseButton.LeftButton, _Qt.KeyboardModifier.NoModifier
for _kind, _x, _btn in ((_ME.Type.MouseButtonPress, 300, _B), (_ME.Type.MouseMove, 302, _B),
                        (_ME.Type.MouseButtonRelease, 302, _B)):
    _ev = _ME(_kind, _P(_x, 6), _P(_x, 6), _btn, _B if _kind != _ME.Type.MouseButtonRelease else _Qt.MouseButton.NoButton, _NB)
    app.sendEvent(rp.view, _ev)
app.processEvents()
results["bar_click_stays"] = not _slides and win.mode.center == 100.5e6
rp.view.slid.disconnect(_slides.append)
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
