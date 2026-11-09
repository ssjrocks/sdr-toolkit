"""Headless window check: every mode opens and runs; three decode a test signal.

    QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py [screenshot-folder]
"""

import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
errors = []


def hook(*a):
    errors.append(a)
    traceback.print_exception(*a)


sys.excepthook = hook

import numpy as np  # noqa: E402
from PyQt6.QtCore import QSettings  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import signals  # noqa: E402
from sdr_toolkit import appreports, gui  # noqa: E402
from sdr_toolkit.engines.morse.synth import transmission  # noqa: E402
from sdr_toolkit.engines.sstv.modes import BY_NAME  # noqa: E402
from sdr_toolkit.engines.sstv.synth import color_bars, encode  # noqa: E402

appreports.init = lambda *a, **k: None  # never send test failures as real reports
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
shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None

app = QApplication([])
win = gui.MainWindow(Path(tempfile.mkdtemp()))
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
    if M.id == "lora" and lora_available() is None:
        assert not win.running and "build-gr-lora-sdr.sh" in win.title.text(), win.title.text()
        continue
    if M.id == "dmr":
        # No default frequency: it asks for one (or for DSD-FME to be built) instead of starting.
        assert not win.running and ("frequency" in win.title.text() or "build-dsd-fme" in win.title.text())
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
win.close()
print(results, "errors:", len(errors))
ok = all(results.values()) and not errors
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
