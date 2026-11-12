"""Main window: mode list, waterfall, decoded output, settings and tuning help."""

import collections
import html
import json
import logging
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QFont, QFontDatabase, QImage, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import APP_ID, APP_NAME, __version__, aircraftdb, appreports, coverage, deps, updater
from .explorer_view import ExplorerPane
from .satellites.view import SatellitePanel
from .widgets import ORDER, BoxCombo, FitLabel, SortItem, drop_oldest, filling, sortable, unsorted
from .capture import AudioCapture, list_outputs
from .logbook import Logbook
from .mapview import MapView
from .modes import GROUPS, MODES
from .modes.base import Event, MissingParts, Mode, Setting
from .panels import DetailsPanel, LogbookWindow, Photos, RelayDialog
from .receiver.view import ReceiverPanel

log = logging.getLogger(__name__)

WATERFALL_ROWS = 160
FFT_SIZE = 2048


def to_qimage(pixels: np.ndarray) -> QImage:
    h, w, _ = pixels.shape
    data = np.ascontiguousarray(pixels)
    return QImage(data.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


class Waterfall(QWidget):
    """Spectrum line over a scrolling waterfall; click to tune the mode."""

    clicked = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self.lo, self.hi = 0.0, 4000.0
        self.markers: list[tuple[float, str]] = []
        self._rows = np.zeros((WATERFALL_ROWS, 512), np.float32) - 100
        self._line = None
        self._floor = -90.0
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Audio spectrum and waterfall. Click a signal to tune the decoder to it.")
        # Colour map: black, blue, cyan, yellow, white.
        stops = np.array([[0, 0, 0], [0, 0, 160], [0, 180, 220], [250, 230, 0], [255, 255, 255]], float)
        x = np.linspace(0, len(stops) - 1, 256)
        i = np.minimum(x.astype(int), len(stops) - 2)
        f = (x - i)[:, None]
        self._cmap = ((1 - f) * stops[i] + f * stops[i + 1]).astype(np.uint8)

    def set_range(self, lo, hi):
        self.lo, self.hi = float(lo), float(hi)
        self._rows[:] = -100
        self._line = None

    def add(self, audio: np.ndarray, rate: int):
        if len(audio) < FFT_SIZE:
            return
        x = audio[-FFT_SIZE:] * np.hanning(FFT_SIZE)
        p = np.abs(np.fft.rfft(x)) ** 2 / (FFT_SIZE * FFT_SIZE / 8)
        freqs = np.fft.rfftfreq(FFT_SIZE, 1 / rate)
        cols = np.linspace(self.lo, self.hi, self._rows.shape[1])
        db = 10 * np.log10(np.interp(cols, freqs, p) + 1e-12)
        self._floor = 0.98 * self._floor + 0.02 * float(np.percentile(db, 20))
        self._rows = np.roll(self._rows, 1, axis=0)
        self._rows[0] = db
        self._line = db if self._line is None else 0.5 * self._line + 0.5 * db
        self.update()

    def _x(self, f):
        return (f - self.lo) / (self.hi - self.lo) * self.width()

    def mousePressEvent(self, ev):
        f = self.lo + ev.position().x() / max(self.width(), 1) * (self.hi - self.lo)
        self.clicked.emit(f)

    def paintEvent(self, _ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        top = max(40, h // 3)
        pal = self.palette()
        p.fillRect(0, 0, w, top, pal.base())
        # Waterfall
        lo = self._floor - 3
        v = np.clip((self._rows - lo) / 45.0, 0, 1)
        rgb = self._cmap[(v * 255).astype(np.uint8)]
        img = to_qimage(np.ascontiguousarray(rgb))
        p.drawImage(self.rect().adjusted(0, top, 0, 0), img)
        # Spectrum line
        if self._line is not None:
            ys = top - 2 - np.clip((self._line - lo) / 60.0, 0, 1) * (top - 14)
            xs = np.linspace(0, w, len(ys))
            p.setPen(QPen(pal.text().color(), 1))
            for i in range(1, len(xs)):
                p.drawLine(int(xs[i - 1]), int(ys[i - 1]), int(xs[i]), int(ys[i]))
        # Scale and markers
        small = QFont(self.font())
        small.setPointSizeF(small.pointSizeF() * 0.8)
        p.setFont(small)
        p.setPen(pal.placeholderText().color())
        step = 500 if self.hi - self.lo > 2500 else 250 if self.hi - self.lo > 1000 else 100
        f = (int(self.lo) // step + 1) * step
        while f < self.hi:
            p.drawText(int(self._x(f)) + 2, 11, f"{f}")
            f += step
        for f, label in self.markers:
            x = int(self._x(f))
            p.setPen(QPen(QColor(255, 140, 0), 1, Qt.PenStyle.DashLine))
            p.drawLine(x, 12, x, h)
            if label:
                p.setPen(QColor(255, 140, 0))
                p.drawText(x + 3, 24, label)
        p.end()


class PictureView(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 240)
        self.image: QImage | None = None
        self.row = None
        self.aspect = None
        self.placeholder = "Waiting for a picture…"

    def show_image(self, image, row=None, aspect=None):
        self.image, self.row, self.aspect = image, row, aspect
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(24, 24, 24))
        if self.image is None:
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            p.end()
            return
        iw, ih = self.image.width(), self.image.height()
        aspect = self.aspect or iw / ih
        w = min(self.width(), int(self.height() * aspect))
        h = int(w / aspect)
        if self.aspect is None and h > self.height():
            h = self.height()
            w = int(h * aspect)
        x, y = (self.width() - w) // 2, (self.height() - h) // 2
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(self.rect().adjusted(x, y, -(self.width() - w - x), -(self.height() - h - y)), self.image)
        if self.row is not None and self.row < ih:
            ly = y + int(self.row / ih * h)
            p.setPen(QPen(QColor(255, 140, 0), 1))
            p.drawLine(x, ly, x + w, ly)
        p.end()


class SettingsForm(QWidget):
    """A form built from a mode's settings."""

    changed = pyqtSignal(str, object)

    def __init__(self, settings: tuple[Setting, ...], values: dict):
        super().__init__()
        self.widgets = {}
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for s in settings:
            w = self._make(s, values[s.key])
            if s.help:
                w.setToolTip(s.help)
            self.widgets[s.key] = (s, w)
            if s.kind == "bool":
                form.addRow(w)
            else:
                label = QLabel(s.label)
                if s.help:
                    label.setToolTip(s.help)
                form.addRow(label, w)
            if s.help:
                note = QLabel(s.help)
                note.setWordWrap(True)
                note.setStyleSheet("color: palette(placeholder-text); font-size: small")
                form.addRow(note)

    def _make(self, s: Setting, value):
        if s.kind == "bool":
            w = QCheckBox(s.label)
            w.setChecked(bool(value))
            w.toggled.connect(lambda v, k=s.key: self.changed.emit(k, bool(v)))
        elif s.kind == "int":
            w = QSpinBox()
            w.setRange(int(s.minimum), int(s.maximum))
            w.setSingleStep(int(s.step))
            w.setSuffix(s.suffix)
            w.setValue(int(value))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda v, k=s.key: self.changed.emit(k, int(v)))
        elif s.kind == "float":
            w = QDoubleSpinBox()
            w.setDecimals(s.decimals)
            w.setRange(s.minimum, s.maximum)
            w.setSingleStep(s.step)
            w.setSuffix(s.suffix)
            w.setValue(float(value))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda v, k=s.key: self.changed.emit(k, float(v)))
        elif s.kind == "choice":
            w = BoxCombo()
            for val, label in s.choices:
                w.addItem(label, val)
            i = next((n for n, (val, _) in enumerate(s.choices) if val == value), 0)
            w.setCurrentIndex(i)
            w.currentIndexChanged.connect(lambda i, k=s.key, c=s.choices: self.changed.emit(k, c[i][0]))
        else:
            w = QLineEdit(str(value))
            w.editingFinished.connect(lambda k=s.key, w=None: self.changed.emit(k, self.widgets[k][1].text()))
        return w

    def set_value(self, key, value):
        s, w = self.widgets[key]
        w.blockSignals(True)
        if s.kind == "bool":
            w.setChecked(bool(value))
        elif s.kind in ("int", "float"):
            w.setValue(value)
        elif s.kind == "choice":
            w.setCurrentIndex(next((n for n, (val, _) in enumerate(s.choices) if val == value), 0))
        else:
            w.setText(str(value))
        w.blockSignals(False)


def tuning_html(mode: Mode) -> str:
    t = mode.tuning
    rows = "".join(f"<tr><td><b>{html.escape(a)}</b></td><td>&nbsp;{html.escape(b)}</td></tr>"
                   for a, b in t.frequencies)
    needs = ""
    for part in mode.requires:
        name, installed, _ = deps.PARTS[part]
        needs += (f"<p>Uses {html.escape(name)}: "
                  + ("installed." if installed() else "<span style='color:#c33'>not installed yet</span>; "
                     + ("use <b>Install</b> above the output." if deps.can_install()
                        else "use <b>How to get it</b> above the output.")) + "</p>")
    return (f"<h3>{html.escape(mode.name)}</h3><p>{html.escape(mode.summary)}</p>"
            f"<p><b>Select on the SDR:</b> {html.escape(t.sdr_mode)}</p>"
            f"<p><b>Bandwidth / filter:</b> {html.escape(t.bandwidth)}</p>"
            + (f"<p><b>Where to listen:</b></p><table>{rows}</table>" if rows else "")
            + (f"<p>{html.escape(t.notes)}</p>" if t.notes else "") + needs)


class BackgroundTask(QThread):
    """Runs a function off the GUI thread; progress(str) may be called from it."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.succeeded.emit(self.fn(self.progress.emit))
        except Exception as e:  # shown to the user by the caller
            log.exception("background task failed")
            self.failed.emit(str(e))


class InstallDialog(QDialog):
    """Installs missing parts and shows how it's going."""

    def __init__(self, parent, parts, accept_mbelib):
        super().__init__(parent)
        self.setWindowTitle("Installing")
        self.resize(640, 360)
        self.parts = parts
        lay = QVBoxLayout(self)
        self.heading = QLabel(f"Installing {deps.describe(parts)}. You'll be asked for your password; "
                              "building LoRa and DMR/P25 support takes a few minutes.")
        self.heading.setWordWrap(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        self.buttons.setEnabled(False)
        lay.addWidget(self.heading)
        lay.addWidget(self.log, 1)
        lay.addWidget(self.buttons)
        self.still_missing = list(parts)
        self.task = BackgroundTask(lambda emit: deps.install(parts, accept_mbelib, emit))
        self.task.progress.connect(self._line)
        self.task.succeeded.connect(self._done)
        self.task.failed.connect(lambda msg: self._done(None, msg))
        self.task.start()

    def _line(self, line):
        if line.startswith("STEP "):
            self.log.appendHtml(f"<b>{html.escape(line[5:])}</b>")
        else:
            self.log.appendHtml(f"<span style='color:gray'>{html.escape(line)}</span>")

    def _done(self, missing, error=None):
        self.still_missing = list(self.parts) if missing is None else missing
        if error:
            self.heading.setText(f"Installing stopped: {error}")
        elif self.still_missing:
            self.heading.setText(f"Not installed: {deps.describe(self.still_missing)}. "
                                 f"The details are in {deps.log_file()}.")
        else:
            self.heading.setText("Everything is installed.")
        self.buttons.setEnabled(True)

    def reject(self):
        if self.task.isRunning():
            return              # an install can't be stopped half way
        super().reject()


class LogWindow(QDialog):
    """A rolling log in its own window: decoded data, or the decoder's raw output."""

    KEEP = 5000

    def __init__(self, parent, title, empty_note):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window)
        self.setWindowTitle(title)
        self.resize(820, 420)
        self.lines = collections.deque(maxlen=self.KEEP)
        self.empty_note = empty_note
        self._held = []
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(self.KEEP)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setPlaceholderText(empty_note)
        self.pause = QPushButton("Pause")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self._on_pause)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        save = QPushButton("Save…")
        save.clicked.connect(self.save)
        row = QHBoxLayout()
        row.addWidget(self.pause)
        row.addWidget(clear)
        row.addStretch(1)
        row.addWidget(save)
        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addLayout(row)

    def add(self, text):
        # Some modes' lines start with their own time already.
        stamped = len(text) >= 8 and text[2] == ":" and text[5] == ":" and text[:2].isdigit()
        line = text if stamped else f"{time.strftime('%H:%M:%S')}  {text}"
        self.lines.append(line)
        if self.pause.isChecked():
            self._held.append(line)
            self.pause.setText(f"Resume ({len(self._held)} new)")
        elif self.isVisible():
            self._append(line)

    def _append(self, line):
        bar = self.view.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        self.view.appendPlainText(line)
        if follow:
            bar.setValue(bar.maximum())

    def _on_pause(self, on):
        if not on:
            for line in self._held:
                self._append(line)
            self._held = []
            self.pause.setText("Pause")

    def showEvent(self, ev):
        # Fill in what arrived while the window was closed.
        self.view.setPlainText("\n".join(self.lines))
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())
        super().showEvent(ev)

    def clear(self):
        self.lines.clear()
        self._held = []
        self.view.clear()

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log", str(Path.home() / "sdr-toolkit-log.txt"),
                                              "Text (*.txt)")
        if path:
            Path(path).write_text("\n".join(self.lines) + "\n", encoding="utf-8")


POINT_NAMES = {"dmr": "DMR", "pager": "Pager", "aprs": "APRS", "tones": "Tones"}


class DecodeArea(QWidget):
    """What a decoder makes of the receiver's audio: its own audio waterfall, then text, a table
    or a picture, as on that decoder's own page."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.M = None
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.title = QLabel()
        self.title.setWordWrap(True)                  # rich text: wrapped rather than cut
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.settings_btn = QPushButton("Decoder settings…")
        self.settings_btn.clicked.connect(win.decoder_settings)
        self.stop_btn = QPushButton("Stop decoding")
        self.stop_btn.clicked.connect(lambda: win._receiver_changed("decoder", ""))
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.title, 1)
        head.addWidget(self.settings_btn)
        head.addWidget(self.stop_btn)
        self.waterfall = Waterfall()
        self.waterfall.setMinimumHeight(90)
        self.waterfall.setMaximumHeight(140)
        self.waterfall.setToolTip("The decoder's audio. Click a tone to set the decoder to it.")
        self.waterfall.clicked.connect(win.decoder_click)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono)
        self.text.setMaximumBlockCount(5000)
        self.live = FitLabel("", keep_end=True)   # a long message mustn't widen the window
        self.live.setFont(mono)
        self.table = sortable(QTableWidget(0, 0))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setFont(mono)
        self._arrivals = 0
        self.picture = PictureView()
        text_page = QWidget()
        tl = QVBoxLayout(text_page)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.text, 1)
        tl.addWidget(self.live)
        self.stack = QStackedWidget()
        for w in (text_page, self.table, self.picture):
            self.stack.addWidget(w)
        # Decode points: more channels decoded at once, each line marked with its frequency.
        self.points_title = FitLabel("")
        self.stop_points_btn = QPushButton("Stop all channels")
        self.stop_points_btn.clicked.connect(win.remove_all_points)
        self.points_text = QPlainTextEdit()
        self.points_text.setReadOnly(True)
        self.points_text.setFont(mono)
        self.points_text.setMaximumBlockCount(5000)
        self.points_box = QWidget()
        pl = QVBoxLayout(self.points_box)
        pl.setContentsMargins(0, 4, 0, 0)
        ph = QHBoxLayout()
        ph.addWidget(self.points_title, 1)
        ph.addWidget(self.stop_points_btn)
        pl.addLayout(ph)
        pl.addWidget(self.points_text, 1)
        self.points_box.setVisible(False)
        self._points: list = []
        self.points_hidden = False               # the explorer shows them in its own lists
        self.main = QWidget()
        ml = QVBoxLayout(self.main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.addLayout(head)
        ml.addWidget(self.waterfall)
        ml.addWidget(self.stack, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addWidget(self.main, 3)
        lay.addWidget(self.points_box, 2)
        self.setVisible(False)

    def show_decoder(self, M):
        """Set up for decoder class M (None hides its part of the area)."""
        self.M = M
        self.main.setVisible(M is not None)
        self.setVisible(M is not None or bool(self._points))
        if M is None:
            return
        self.title.setText(f"<b>Decoding with {html.escape(M.name)}</b>")
        self.text.clear()
        self.live.setText("")
        self.picture.show_image(None)
        hi = min(M.rate / 2, 3500 if M.rate <= 12000 else 4000)
        self.waterfall.set_range(0, hi)
        if M.output == "table":
            self.table.clear()
            self.table.setRowCount(0)
            unsorted(self.table)
            self.table.setColumnCount(len(M.columns))
            self.table.setHorizontalHeaderLabels([h for _, h in M.columns])
            self.table.horizontalHeader().setStretchLastSection(True)
            self.stack.setCurrentWidget(self.table)
        elif M.output == "image":
            self.stack.setCurrentWidget(self.picture)
        else:
            self.stack.setCurrentIndex(0)

    def handle(self, e):
        """One event from the decoder; returns text for the logs, if any."""
        M = self.M
        if M is None:
            return None
        if e.kind == "line":
            color = {"dim": "gray", "warn": "#c33"}.get(e.style)
            text = html.escape(e.text).replace(" ", "&nbsp;") or "&nbsp;"
            self.text.appendHtml(f"<span style='color:{color}'>{text}</span>" if color else text)
            if M.output != "text" and e.style == "warn":
                self.title.setText(f"<b>{html.escape(M.name)}</b>: <span style='color:#c33'>{html.escape(e.text)}</span>")
            return e.text.strip() if e.style not in ("warn", "dim") else None
        if e.kind == "live":
            self.live.setText(e.text)
        elif e.kind == "row":
            t = self.table
            with filling(t):
                r = t.rowCount()
                t.insertRow(r)
                for c, (key, _) in enumerate(M.columns):
                    item = SortItem(str(e.data.get(key, "")))
                    if c == 0:
                        self._arrivals += 1
                        item.setData(ORDER, self._arrivals)
                    t.setItem(r, c, item)
                if t.rowCount() > 2000:
                    drop_oldest(t)
            t.scrollToBottom()
            return "  ".join(f"{h}: {e.data[k]}" for k, h in M.columns if str(e.data.get(k, "")) != "")
        elif e.kind == "image":
            self.picture.show_image(to_qimage(e.image), e.row, getattr(M, "aspect", None))
            self.title.setText(f"<b>Decoding with {html.escape(M.name)}</b> · {html.escape(e.title)}")
            return e.title if e.done else None
        elif e.kind == "log":
            return e.text if e.style != "pos" else None
        return None

    def set_points(self, states):
        """The decode points (ReceiverMode.point_states()), for the pane's heading."""
        self._points = states
        shown = bool(states) and not self.points_hidden
        self.points_box.setVisible(shown)
        self.setVisible(self.M is not None or shown)
        now = time.time()
        parts = [f"{s['freq'] / 1e6:.4f} {POINT_NAMES.get(s['decoder'], s['decoder'])}"
                 + (" ●" if now - s["heard"] < 5 else "") + ("" if s["in_view"] else " (outside the band)")
                 + (" (muted)" if s["muted"] else "") for s in states]
        self.points_title.setText(f"Decoding {len(states)} channel{'s' if len(states) != 1 else ''}: "
                                  + ", ".join(parts))

    def point_event(self, freq, decoder, e):
        """One event from a decode point's decoder; returns text for the logs, if any."""
        if e.kind == "row":
            text = "  ".join(f"{v}" for k, v in e.data.items() if str(v) != "" and k not in ("time",))
        elif e.kind in ("line", "log") and e.text and e.style != "pos":
            text = e.text.strip()
        else:
            return None
        line = f"{freq / 1e6:.4f}  {POINT_NAMES.get(decoder, decoder):<6} {text}"
        color = {"dim": "gray", "warn": "#c33"}.get(e.style)
        esc = html.escape(line).replace(" ", "&nbsp;")
        self.points_text.appendHtml(f"<span style='color:{color}'>{esc}</span>" if color else esc)
        return line if e.style not in ("warn", "dim") else None

    def audio(self, tail, rate, markers):
        if len(tail):
            self.waterfall.add(tail, rate)
            self.waterfall.markers = markers


class MainWindow(QMainWindow):
    report_finished = pyqtSignal(bool)

    def __init__(self, state_dir: Path):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.settings = QSettings("ssjrocks", "sdr-toolkit")
        self.mode: Mode | None = None
        self.capture: AudioCapture | None = None
        self.running = False
        self._tail = np.zeros(0, np.float32)
        self._last_image = None
        self.logbook = Logbook()
        self.logbook.prune()
        self.photos = Photos(self)
        self._selected = None
        self._last_details = 0.0
        self._last_flush = 0.0
        self.coverage = None
        self._coverage_saved = 0.0
        self._coverage_drawn = 0.0

        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        # Mode list
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(190)
        self._items = {}
        for group in GROUPS:
            g = QTreeWidgetItem([group])
            g.setFlags(Qt.ItemFlag.ItemIsEnabled)
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            self.tree.addTopLevelItem(g)
            for M in MODES:
                if M.group == group and not getattr(M, "hidden", False):
                    it = QTreeWidgetItem([M.name])
                    it.setData(0, Qt.ItemDataRole.UserRole, M.id)
                    it.setToolTip(0, M.summary)
                    g.addChild(it)
                    self._items[M.id] = it
            g.setExpanded(True)
        self.tree.currentItemChanged.connect(self._on_tree)

        # Top bar
        self.source_combo = QComboBox()
        self.source_combo.setToolTip("The output to listen to. Default follows your speaker setting.")
        refresh = QToolButton()
        refresh.setText("⟳")
        refresh.setToolTip("Refresh the list of outputs")
        refresh.clicked.connect(self.fill_sources)
        self.start_btn = QPushButton("Start")
        self.start_btn.setCheckable(True)
        self.start_btn.toggled.connect(self.on_start_toggled)
        self.update_btn = QPushButton()
        self.update_btn.setVisible(False)
        self.update_btn.clicked.connect(self.install_update)
        top = QHBoxLayout()
        top.addWidget(QLabel("Listen to"))
        top.addWidget(self.source_combo, 1)
        top.addWidget(refresh)
        top.addWidget(self.start_btn)
        self.relay_btn = QPushButton("Connect relay…")
        self.relay_btn.setToolTip("Use an RTL-SDR on another PC running SDR Tools Relay")
        self.relay_btn.clicked.connect(self.relay_clicked)
        top.addWidget(self.relay_btn)
        top.addWidget(self.update_btn)

        self.waterfall = Waterfall()
        self.waterfall.clicked.connect(self.on_waterfall_click)

        # Output: text, table or picture
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono)
        self.text.setMaximumBlockCount(20000)
        self.live = FitLabel("", keep_end=True)   # a long message mustn't widen the window
        self.live.setFont(mono)
        self.live.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_page = QWidget()
        tl = QVBoxLayout(text_page)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.text, 1)
        tl.addWidget(self.live)
        self.table = sortable(QTableWidget(0, 0))
        self._arrivals = 0
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setFont(mono)
        self.picture = PictureView()
        self.map = MapView()
        self.map.picked.connect(self.select_craft)
        self.table.itemSelectionChanged.connect(self._on_table_pick)
        self.table_page = QSplitter(Qt.Orientation.Vertical)
        self.table_page.addWidget(self.map)
        self.table_page.addWidget(self.table)
        self.table_page.setSizes([420, 260])
        self.output = QStackedWidget()
        self.output.addWidget(text_page)
        self.output.addWidget(self.table_page)
        self.output.addWidget(self.picture)
        self.receiver_panel = ReceiverPanel()
        self.receiver_panel.changed.connect(self._receiver_changed)
        self.decode_area = DecodeArea(self)
        self.receiver_panel.add_decode_area(self.decode_area)
        rp = self.receiver_panel
        rp.identify.connect(lambda: self.mode.identify() if self.running and self.mode.output == "receiver" else None)
        rp.guesses.connect(self._guess_menu)
        rp.tag.connect(lambda: self._tag_dialog(float(self.mode.values.get("freq_mhz", 0)) * 1e6))
        rp.view.slid.connect(self._receiver_slid)
        rp.view.menu.connect(self._receiver_menu)
        rp.survey.connect(self._survey_toggled)
        rp.survey_view.view_changed.connect(lambda lo, hi: self.mode.survey_view(lo, hi)
                                            if self.running and self.mode.output == "receiver" else None)
        rp.survey_view.listen.connect(self._survey_listen)
        self._guesses: list = []
        self._tags_view = None
        self.output.addWidget(self.receiver_panel)
        # The DMR / P25 explorer's lists, under the spectrum on its page.
        self.explorer_pane = ExplorerPane()
        self.explorer_pane.setVisible(False)
        rp.split.addWidget(self.explorer_pane)
        self.explorer_pane.only.connect(self._explorer_only)
        self.explorer_pane.tune.connect(lambda f: self._explorer_only(("freq", f)))
        self.explorer = None                      # receiver/explore.Explorer, loaded with the page
        self._ex_muted: dict = {}
        self._ex_saved = 0.0
        self._ex_timer = QTimer(self)
        self._ex_timer.setInterval(1000)
        self._ex_timer.timeout.connect(self._explorer_tick)
        self.satellite_panel = SatellitePanel(MapView())
        self.satellite_panel.listen.connect(self.listen_satellite)
        self.output.addWidget(self.satellite_panel)
        self._doppler = None                 # the satellite Spectrum & receiver is following
        self._doppler_timer = QTimer(self)
        self._doppler_timer.setInterval(1000)
        self._doppler_timer.timeout.connect(self._doppler_step)
        self.decoded_log = LogWindow(self, "Decoded log", "Everything decoded appears here, with the time.")
        self.raw_log = LogWindow(self, "Raw data", "The decoder's raw output appears here.")
        self.title = QLabel("")
        self.title.setWordWrap(True)

        # Side panel: settings, how to tune
        self.side = QTabWidget()
        self.settings_area = QScrollArea()
        self.settings_area.setWidgetResizable(True)
        self.help_label = QLabel()
        self.help_label.setWordWrap(True)
        self.help_label.setTextFormat(Qt.TextFormat.RichText)
        self.help_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.help_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        help_area = QScrollArea()
        help_area.setWidgetResizable(True)
        help_area.setWidget(self.help_label)
        self.side.addTab(help_area, "How to tune")
        self.side.addTab(self.settings_area, "Settings")
        self.details = DetailsPanel(self.photos)
        self.side.addTab(self.details, "Details")
        self.side.setMinimumWidth(300)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_output)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(self.copy_output)
        save_btn = QPushButton("Save…")
        save_btn.clicked.connect(self.save_output)
        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.clicked.connect(self.open_folder)
        updates_btn = QPushButton("Check for updates")
        updates_btn.clicked.connect(self.check_for_updates)
        report_btn = QPushButton("Report a problem…")
        report_btn.clicked.connect(self.report_problem)
        btns = QHBoxLayout()
        decoded_btn = QPushButton("Decoded log")
        decoded_btn.setToolTip("A rolling, timestamped log of everything decoded, in its own window")
        decoded_btn.clicked.connect(lambda: self._show_log(self.decoded_log))
        raw_btn = QPushButton("Raw data")
        raw_btn.setToolTip("What the decoder outputs before the toolkit tidies it up, in its own window")
        raw_btn.clicked.connect(lambda: self._show_log(self.raw_log))
        logbook_btn = QPushButton("Logbook")
        logbook_btn.setToolTip("Everything received, the aircraft and ships seen, and replays of their movements")
        logbook_btn.clicked.connect(self.show_logbook)
        for b in (clear_btn, copy_btn, save_btn, self.folder_btn, decoded_btn, raw_btn, logbook_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(updates_btn)
        btns.addWidget(report_btn)

        middle = QWidget()
        ml = QVBoxLayout(middle)
        ml.setContentsMargins(0, 0, 0, 0)
        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.addWidget(self.waterfall)
        out = QWidget()
        ol = QVBoxLayout(out)
        ol.setContentsMargins(0, 0, 0, 0)
        self.install_bar = QFrame()
        self.install_bar.setFrameShape(QFrame.Shape.StyledPanel)
        ib = QHBoxLayout(self.install_bar)
        self.install_label = QLabel("")
        self.install_label.setWordWrap(True)
        self.install_btn = QPushButton("Install" if deps.can_install() else "How to get it")
        self.install_btn.clicked.connect(self.install_missing)
        ib.addWidget(self.install_label, 1)
        ib.addWidget(self.install_btn)
        self.install_bar.setVisible(False)
        self._to_install: list[str] = []
        self._mode_specific = False
        self._tasks = set()
        self._update = None
        ol.addWidget(self.install_bar)
        self.action_bar = QWidget()
        self._action_layout = QHBoxLayout(self.action_bar)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_buttons: dict[str, QPushButton] = {}
        self._last_actions = 0.0
        self.action_bar.setVisible(False)
        ol.addWidget(self.action_bar)
        ol.addWidget(self.title)
        ol.addWidget(self.output, 1)
        vsplit.addWidget(out)
        vsplit.setStretchFactor(0, 1)
        vsplit.setStretchFactor(1, 3)
        ml.addWidget(vsplit, 1)
        ml.addLayout(btns)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(middle)
        split.addWidget(self.side)
        split.setStretchFactor(1, 1)
        split.setSizes([200, 700, 330])

        central = QWidget()
        cl = QVBoxLayout(central)
        cl.addLayout(top)
        cl.addWidget(split, 1)
        self.setCentralWidget(central)
        self.status = FitLabel("")
        self.statusBar().addWidget(self.status, 1)
        self.level_label = QLabel("")
        self.statusBar().addPermanentWidget(self.level_label)

        self.report_finished.connect(self._on_report_finished)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)

        self.fill_sources()
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 800)
        first = self.settings.value("mode", "morse")
        self.tree.setCurrentItem(self._items.get(first, self._items["morse"]))
        QTimer.singleShot(0, self.startup_checks)

    # ---- sources -------------------------------------------------------

    def fill_sources(self):
        current = (self.source_combo.currentData() if self.source_combo.count()
                   else self.settings.value("source", ""))
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("Default output (follows your speaker setting)", "")
        for name, desc in list_outputs():
            self.source_combo.addItem(desc, name)
        i = self.source_combo.findData(current or "")
        self.source_combo.setCurrentIndex(max(i, 0))
        self.source_combo.blockSignals(False)
        try:
            self.source_combo.currentIndexChanged.disconnect(self.on_source_changed)
        except TypeError:
            pass
        self.source_combo.currentIndexChanged.connect(self.on_source_changed)

    def on_source_changed(self, _i):
        self.settings.setValue("source", self.source_combo.currentData() or "")
        if self.running:
            self._restart_capture()

    # ---- modes ---------------------------------------------------------

    def _on_tree(self, item, _prev):
        mode_id = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if mode_id is None:
            return
        self.select_mode(mode_id)

    @staticmethod
    def _key(M, key) -> str:
        """Where a setting is stored: shared ones once for every mode, the rest per mode."""
        s = next((s for s in M.settings if s.key == key), None)
        return f"radio/{key}" if s is not None and s.shared else f"{M.id}/{key}"

    def _values(self, M) -> dict:
        values = {}
        for s in M.settings:
            raw = self.settings.value(self._key(M, s.key), None)
            values[s.key] = s.default if raw is None else self._convert(s, raw)
        return values

    @staticmethod
    def _convert(s: Setting, raw):
        try:
            if s.kind == "bool" or isinstance(s.default, bool):
                return raw in (True, "true", "1", 1)
            if isinstance(s.default, float):
                return float(raw)
            if isinstance(s.default, int):
                return int(float(raw))
            return str(raw)
        except (TypeError, ValueError):
            return s.default

    def select_mode(self, mode_id):
        if self.running:
            self._stop()
        M = next(M for M in MODES if M.id == mode_id)
        self.mode = M()
        self.mode.values = self._values(M)
        self.settings.setValue("mode", mode_id)
        self.help_label.setText(tuning_html(self.mode))
        form = SettingsForm(M.settings, self.mode.values)
        form.changed.connect(self.on_setting)
        self.form = form
        self.settings_area.setWidget(form)
        self._setup_output()
        hi = min(self.mode.rate / 2, 3500 if self.mode.rate <= 12000 else 4000)
        self.waterfall.set_range(0, hi)
        self.waterfall.markers = self.mode.markers()
        self.folder_btn.setVisible("folder" in self.mode.values)
        self.map.setVisible(getattr(self.mode, "map", False))
        self.map.clear_session()
        self.map.set_points([], None)
        self.map.follow = True
        self._selected = None
        self.coverage = None
        self.details.clear()
        self.side.setTabVisible(self.side.indexOf(self.details), bool(self.mode.key_column))
        if self.side.currentWidget() is self.details and not self.mode.key_column:
            self.side.setCurrentIndex(0)
        if self.mode.id == "adsb" and aircraftdb.due():
            self.fetch_aircraft_db()
        for w in (self.decoded_log, self.raw_log):
            w.add(f"──── {self.mode.name} ────")
        self.raw_log.setWindowTitle(f"Raw data: {self.mode.name}")
        self.decoded_log.setWindowTitle(f"Decoded log: {self.mode.name}")
        # Radio modes read the RTL-SDR or a recording, not the speakers.
        radio = self._is_radio() or getattr(self.mode, "source", "audio") == "none"
        self.level_label.setText("")
        self.source_combo.setEnabled(not radio)
        self.waterfall.setVisible(not radio)
        self.source_combo.setToolTip("RTL-SDR modes don't use this: see Settings → Input." if radio else
                                     "The output to listen to. Default follows your speaker setting.")
        self._mode_specific = False
        self.show_general_missing()
        # Listening starts with the mode: pick one and play the signal.
        self.start_btn.setChecked(True)

    def _setup_output(self):
        m = self.mode
        self.title.setText("")
        self._warned = False
        self.live.setText("")
        self.text.clear()
        self.picture.show_image(None)
        self._last_image = None
        if m.output == "table":
            self.table.clear()
            self.table.setRowCount(0)
            unsorted(self.table)
            self.table.setColumnCount(len(m.columns))
            self.table.setHorizontalHeaderLabels([h for _, h in m.columns])
            hh = self.table.horizontalHeader()
            for i in range(len(m.columns) - 1):
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
            hh.setSectionResizeMode(len(m.columns) - 1, QHeaderView.ResizeMode.Stretch)
            self.output.setCurrentWidget(self.table_page)
        elif m.output == "receiver":
            self.receiver_panel.show_values(m.values)
            self.output.setCurrentWidget(self.receiver_panel)
            explorer = getattr(m, "explorer", False)
            self.explorer_pane.setVisible(explorer)
            self.decode_area.points_hidden = explorer      # the explorer's own lists show them
        elif m.output == "satellites":
            self._satellite_settings()
            self.output.setCurrentWidget(self.satellite_panel)
        elif m.output == "image":
            self.output.setCurrentWidget(self.picture)
        else:
            self.output.setCurrentWidget(self.output.widget(0))

    def on_setting(self, key, value):
        if self.mode is None:
            return
        self.settings.setValue(self._key(self.mode, key), value)
        values = dict(self.mode.values)
        values[key] = value
        setting = next(s for s in self.mode.settings if s.key == key)
        if self.running and setting.restart:
            self._stop_mode()
            self.mode.values = values
            self._start_mode()
        elif self.running:
            self.mode.apply(values)
        else:
            self.mode.values = values
        self.waterfall.markers = self.mode.markers()
        self.waterfall.update()
        if self.mode.output == "satellites":
            self._satellite_settings()
        if self.mode.output == "receiver":
            if key == "demod":                 # each mode starts at its usual bandwidth
                from .receiver.dsp import MODES
                bw = MODES[value][0]
                self.form.set_value("bandwidth", bw)
                self.on_setting("bandwidth", bw)
            if key == "decoder":
                self._receiver_decoder(set_demod=True)
            self.receiver_panel.show_values(self.mode.values)

    def _receiver_decoder(self, set_demod=False):
        """Start the receiver's chosen decoder (with its saved settings) and show its area."""
        from .receiver.decode import DECODERS
        mode_id = self.mode.values.get("decoder", "")
        M = next((m for m in MODES if m.id == mode_id), None)
        if M is not None and set_demod:
            demod, bw = DECODERS[mode_id]
            for k, val in (("demod", demod), ("bandwidth", bw)):
                if self.mode.values.get(k) != val:
                    self.form.set_value(k, val)
                    self.settings.setValue(self._key(self.mode, k), val)
                    values = dict(self.mode.values, **{k: val})
                    if self.running:
                        self.mode.apply(values)
                    else:
                        self.mode.values = values
        self.decode_area.show_decoder(M)
        # _start_mode() calls this before the window counts as running: the mode has started then.
        if self.running or getattr(self, "_mode_started", False):
            self.mode.set_decoder(mode_id, self._values(M) if M else {})

    # ---- identifying, tagging, sliding ---------------------------------

    def _show_identifying(self, state):
        """Identify's progress on its button: listening to the channel, then trying the decoders."""
        b = self.receiver_panel.identify_btn
        stage = state["stage"]
        if stage == "listening":
            b.setText(f"Listening… {state['left']:.0f} s")
            b.setEnabled(False)
            self.status.setText(f"Identify: recording {state['freq'] / 1e6:.4f} MHz for the decoders")
        elif stage == "trying":
            b.setText(f"Decoding… {state['done']}/{state['total']}" if state["total"] else "Decoding…")
            b.setEnabled(False)
        else:
            b.setText("Identify")
            b.setEnabled(True)
            self.status.setText("Identify stopped: the frequency changed while it was listening")

    def _show_guesses(self, found):
        self._guesses = found["guesses"]
        self._verified = found.get("verified", [])
        self._tried = found.get("tried", [])
        if not found["auto"]:
            self.receiver_panel.identify_btn.setText("Identify")
            self.receiver_panel.identify_btn.setEnabled(True)
        g = self._guesses[0] if self._guesses else None
        btn = self.receiver_panel.looks
        if g is not None:
            what = "Identified" if self._verified else "Looks like"
            btn.setText(f"{what}: {g.name} ({g.confidence:.0%}) at {found['freq'] / 1e6:.4f} MHz")
            btn.setToolTip(f"{g.why}\nClick for all guesses and what to do.")
        if not found["auto"]:
            if self._verified:
                self.status.setText(f"Identified {found['freq'] / 1e6:.4f} MHz: {self._verified[0].name}")
            elif self._tried:
                self.status.setText(f"No decoder recognised {found['freq'] / 1e6:.4f} MHz "
                                    f"(tried {', '.join(self._tried)}); the guesses are from its measurements")
            self._guess_menu(btn)

    def _apply_guess(self, g):
        if g.mode:
            self.tree.setCurrentItem(self._items[g.mode])
            return
        if g.demod:
            self._receiver_changed("demod", g.demod)
        if g.bandwidth:
            self._receiver_changed("bandwidth", int(g.bandwidth))
        if g.decoder and g.decoder != self.mode.values.get("decoder"):
            self._receiver_changed("decoder", g.decoder)

    def _guess_menu(self, anchor):
        if not self._guesses:
            QMessageBox.information(self, "Identify", "Nothing identified yet: tune to a signal and press Identify.")
            return
        menu = QMenu(self)
        names = {M.id: M.name for M in MODES}
        verified = getattr(self, "_verified", [])
        tried = getattr(self, "_tried", [])
        if tried and not verified:
            note = menu.addAction(f"No decoder recognised it (tried {', '.join(tried)}). From its measurements:")
            note.setEnabled(False)
            menu.addSeparator()
        for g in self._guesses[:5 + len(verified)]:
            head = menu.addAction(f"✓ {g.name}" if g in verified else f"{g.name} — {g.confidence:.0%}")
            head.setEnabled(False)
            f = head.font()
            f.setBold(True)
            head.setFont(f)
            why = menu.addAction(f"    {g.why}")
            why.setEnabled(False)
            if g.mode:
                menu.addAction(f"    Open {names.get(g.mode, g.mode)}", lambda g=g: self._apply_guess(g))
            elif g.demod:
                what = f"    Listen as {g.demod} {g.bandwidth / 1000:g} kHz"
                if g.decoder:
                    what += f" and decode with {names.get(g.decoder, g.decoder)}"
                menu.addAction(what, lambda g=g: self._apply_guess(g))
            if g.confidence >= 0.4 and g.name != "nothing much here":
                menu.addAction("    Tag as this", lambda g=g: self._tag_dialog(
                    float(self.mode.values.get("freq_mhz", 0)) * 1e6, g.name.split(" (")[0], g))
            menu.addSeparator()
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _refresh_tags(self):
        view = self.receiver_panel.view
        lo, hi = view.center - view.rate, view.center + view.rate
        view.tags = self.logbook.tags(lo, hi)
        view.update()
        self.receiver_panel.survey_view.tags = self.logbook.tags()
        self.receiver_panel.survey_view.update()

    def _tag_dialog(self, freq, label="", guess=None):
        if self.mode is None or self.mode.output != "receiver" or not freq:
            return
        v = self.mode.values
        suggestions = [label] if label else []
        suggestions += [g.name.split(" (")[0] for g in self._guesses[:3] if g.name.split(" (")[0] not in suggestions]
        suggestions += [x for x in ("DMR", "P25", "APRS", "Pager", "Repeater", "Airband", "Marine", "FM station",
                                    "Beacon", "Noise / interference") if x not in suggestions]
        text, ok = QInputDialog.getItem(self, "Tag this frequency", f"{freq / 1e6:.5f} MHz is:", suggestions, 0, True)
        if not ok or not text.strip():
            return
        g = guess
        self.logbook.add_tag(freq, text.strip(), g.demod if g else v.get("demod", ""),
                             g.bandwidth if g else v.get("bandwidth", 0), g.decoder if g else v.get("decoder", ""))
        self._refresh_tags()
        self.status.setText(f"Tagged {freq / 1e6:.5f} MHz as {text.strip()}")

    def _survey_toggled(self, on):
        if self.mode is None or self.mode.output != "receiver" or not self.running:
            if on:
                self.receiver_panel.survey_btn.setChecked(False)
                self.status.setText("Start the receiver first")
            return
        if on:
            sv = self.receiver_panel.survey_view
            sv.tags = self.logbook.tags()
            self.mode.start_survey(*(sv.sweep or (None, None)))      # what it swept last time, or everything
            self.status.setText("Surveying: zoom and pan to look closer, Sweep what's shown for faster passes, "
                                "double-click to listen")
        else:
            self.mode.stop_survey()
            self.status.setText("Back to the receiver")

    def _survey_listen(self, hz):
        self.receiver_panel.survey_btn.setChecked(False)          # stops the survey
        self._receiver_changed("freq_mhz", round(hz / 1e6, 4))

    def _receiver_slid(self, center):
        if self.mode is None or self.mode.output != "receiver" or not self.running:
            return
        moved = self.mode.set_center(center)
        if moved is not None:
            self.form.set_value("freq_mhz", moved)
            self.settings.setValue(self._key(self.mode, "freq_mhz"), moved)
            self.receiver_panel.show_values(self.mode.values)
        self.status.setText(f"Dongle centred on {center / 1e6:.4f} MHz")

    def _receiver_menu(self, hz, pos):
        if self.mode is None or self.mode.output != "receiver":
            return
        from .receiver.decode import DECODERS
        from .receiver.dsp import MODES as DEMODS
        step = self.mode.values.get("step", 1000) or 1000
        mhz = round(round(hz / step) * step / 1e6, 6)
        menu = QMenu(self)
        names_all = {M.id: M.name for M in MODES}
        menu.addAction(f"Tune to {mhz:.5f} MHz", lambda: self._receiver_changed("freq_mhz", mhz))
        menu.addAction("Identify the signal here", lambda: (self._receiver_changed("freq_mhz", mhz),
                                                             self.mode.identify() if self.running else None))
        menu.addAction("Tag this frequency…", lambda: self._tag_dialog(mhz * 1e6))
        view = self.receiver_panel.view
        lo, hi = view.span()
        near = [t for t in view.tags if abs(t["freq"] - hz) < (hi - lo) / 60]
        for t in near:
            menu.addAction(f"Listen to tag “{t['label']}”", lambda t=t: self._listen_tag(t))
            menu.addAction(f"Remove tag “{t['label']}”", lambda t=t: (self.logbook.delete_tag(t["id"]),
                                                                      self._refresh_tags()))
        from .modes.receiver import POINT_DECODERS
        saved = self._saved_points()
        mine = [p for p in saved if abs(p["freq"] - hz) < max((hi - lo) / 80, 6250)]
        for p in mine:
            name = POINT_NAMES.get(p["decoder"], p["decoder"])
            menu.addAction(f"Stop decoding {p['freq'] / 1e6:.4f} MHz ({name})", lambda p=p: self.remove_point(p["freq"]))
            if p["decoder"] == "dmr":
                menu.addAction(("Unmute" if p.get("muted") else "Mute") + f" {p['freq'] / 1e6:.4f} MHz's voice",
                               lambda p=p: self.mute_point(p["freq"], not p.get("muted")))
        if not mine:
            here = menu.addMenu(f"Decode {mhz:.4f} MHz too (at the same time)")
            for k in POINT_DECODERS:
                here.addAction(names_all.get(k, k), lambda k=k: self.add_point(mhz * 1e6, k))
        tagged = [t for t in self.logbook.tags(lo, hi) if t.get("decoder") in POINT_DECODERS
                  and not any(abs(p["freq"] - t["freq"]) < 1 for p in saved)]
        if tagged:
            menu.addAction(f"Decode the {len(tagged)} tagged channel{'s' if len(tagged) != 1 else ''} in view",
                           lambda: [self.add_point(t["freq"], t["decoder"]) for t in tagged])
        listen = menu.addMenu("Listen as")
        for d in DEMODS:
            listen.addAction(d, lambda d=d: (self._receiver_changed("freq_mhz", mhz), self._receiver_changed("demod", d)))
        names = {M.id: M.name for M in MODES}
        dec = menu.addMenu("Decode with")
        dec.addAction("Nothing", lambda: self._receiver_changed("decoder", ""))
        for k in DECODERS:
            dec.addAction(names.get(k, k), lambda k=k: (self._receiver_changed("freq_mhz", mhz),
                                                        self._receiver_changed("decoder", k)))
        menu.exec(pos)

    # ---- the DMR / P25 explorer ------------------------------------------

    def _explorer_path(self):
        from . import system
        return system.data_dir() / "explorer.json"

    def _explorer_start(self):
        from .receiver.explore import Explorer
        if self.explorer is None:
            self.explorer = Explorer.load(self._explorer_path())
            for t, f, text in self.explorer.log[-200:]:
                self.explorer_pane.add_log(t, f, text)
        self._ex_muted = {}
        self._ex_timer.start()
        self._explorer_tick()

    def _explorer_stop(self):
        self._ex_timer.stop()
        if self.explorer is not None:
            self.explorer.save(self._explorer_path())

    def _explorer_event(self, freq, ev):
        ex = self.explorer
        if ex is None:
            return
        if ev.kind == "explorer":
            ex.found(freq, ev.data["state"])
            if ex.log and ex.log[-1][1] == freq:
                self.explorer_pane.add_log(*ex.log[-1])
            return
        news = ex.add(freq, ev.data)
        if news:
            self.explorer_pane.add_log(time.time(), freq, news)
            self.decoded_log.add(f"{freq / 1e6:.5f}  {news}")
            self.logbook.add("explorer", f"{freq / 1e6:.5f} MHz  {news}")
        if ev.data["what"] == "call":
            self._explorer_listen()

    def _explorer_only(self, choice):
        if self.explorer is None:
            return
        self.explorer.only = choice
        self._explorer_listen()
        self._explorer_tick()

    def _explorer_listen(self):
        """Mute the streams the "listen only to" choice leaves out (again as calls start and stop)."""
        if self.explorer is None or not self.running or not getattr(self.mode, "explorer", False):
            return
        for st in self.mode.point_states():
            f = st["freq"]
            mute = not self.explorer.audible(f)
            if self._ex_muted.get(f) != mute:
                self._ex_muted[f] = mute
                self.mode.mute_point(f, mute)

    def _explorer_tick(self):
        ex = self.explorer
        if ex is None or not getattr(self.mode, "explorer", False):
            return
        only = ex.only
        listening = ("everything" if only is None else f"only {only[1] / 1e6:.5f} MHz" if only[0] == "freq"
                     else f"only talkgroup {only[1]}" if only[0] == "tg" else f"only radio {only[1]}")
        decoding = [st["freq"] for st in self.mode.point_states()] if self.running else []
        self.mode.known_channels = frozenset(f for f, s in ex.states.items() if s == "digital")
        self.explorer_pane.refresh(ex, decoding, listening)
        self._explorer_listen()
        if time.time() - self._ex_saved > 30:
            self._ex_saved = time.time()
            ex.save(self._explorer_path())

    # ---- satellites -----------------------------------------------------

    def _satellite_settings(self):
        from .modes.radio import home
        v = self.mode.values
        self.satellite_panel.set_settings(home(v), v.get("min_el", 10), v.get("days", 5), v.get("only_hear", True))

    def listen_satellite(self, sat, radio):
        """A satellite's downlink in Spectrum & receiver, followed through its Doppler shift."""
        from .satellites import orbit
        here = self.satellite_panel.home
        self.tree.setCurrentItem(self._items["receiver"])        # starts the receiver
        if self.mode is None or self.mode.output != "receiver":
            return
        if radio.decoder == "sattlm":
            # What gr-satellites needs to know (saved as that decoder's settings, which it starts with).
            M = next(m for m in MODES if m.id == "sattlm")
            from .modes.sat_telemetry import generic_modulation
            for key, val in (("satellite", str(sat.norad)), ("sat_name", sat.name),
                             ("modulation", generic_modulation(radio.mode, radio.baud) or "FSK"),
                             ("baud", int(radio.baud or 9600)), ("freq", int(radio.downlink))):
                self.settings.setValue(self._key(M, key), val)
            if self.mode.values.get("decoder") == "sattlm":
                self._receiver_changed("decoder", "")               # restarted with the new satellite
        if radio.decoder:
            self._receiver_changed("decoder", radio.decoder)     # sets its usual demodulator...
        else:
            self._receiver_changed("decoder", "")
        self._receiver_changed("demod", radio.demod)             # ...and then the satellite's
        self._receiver_changed("bandwidth", int(radio.bandwidth))
        f0 = (radio.downlink + radio.downlink_high) / 2 if radio.downlink_high > radio.downlink else radio.downlink
        self._doppler = {"sat": orbit.satrec(sat.line1, sat.line2), "f0": f0, "name": sat.name,
                         "home": here, "last": None}
        self._doppler_step()
        self._doppler_timer.start()

    def _doppler_step(self):
        d = self._doppler
        if d is None or self.mode is None or self.mode.output != "receiver":
            self._stop_doppler()
            return
        if d["last"] is not None and abs(float(self.mode.values.get("freq_mhz", 0)) - d["last"]) > 1e-6:
            self._stop_doppler(f"Stopped following {d['name']}: you tuned elsewhere")
            return
        mhz = d["f0"] / 1e6
        text = f"Listening to {d['name']} on {mhz:.4f} MHz"
        if d["home"] is not None:
            from .satellites import orbit
            tr = orbit.track(d["sat"], [time.time()], d["home"])
            f = orbit.doppler(d["f0"], float(tr.rate[0]))
            mhz = round(f / 1e6, 6)
            state = f"up {tr.el[0]:.0f}°" if tr.el[0] > 0 else "below the horizon"
            text += f", heard {(f - d['f0']) / 1000:+.1f} kHz off (Doppler, {state})"
        else:
            text += " (set your location to follow its Doppler shift)"
        if d["last"] != mhz:
            d["last"] = mhz
            self._receiver_changed("freq_mhz", mhz)
        self.status.setText(text)

    def _stop_doppler(self, why=""):
        self._doppler = None
        self._doppler_timer.stop()
        if why:
            self.status.setText(why)

    # ---- decode points -------------------------------------------------

    def _points_setting(self) -> str:
        """Decode points are kept per page (Spectrum & receiver, the explorer)."""
        return f"{self.mode.id if self.mode is not None else 'receiver'}/points"

    def _saved_points(self) -> list[dict]:
        try:
            return [p for p in json.loads(self.settings.value(self._points_setting(), "[]") or "[]") if "freq" in p]
        except (TypeError, ValueError):
            return []

    def _save_points(self, points):
        self.settings.setValue(self._points_setting(), json.dumps(points))
        self._show_points(force=True)

    def _start_point(self, freq, decoder, muted=False):
        if self.running and self.mode is not None and self.mode.output == "receiver":
            M = next((m for m in MODES if m.id == decoder), None)
            if M is not None:
                self.mode.add_point(freq, decoder, self._values(M), muted)

    def add_point(self, freq, decoder):
        points = self._saved_points()
        if any(abs(p["freq"] - freq) < 1 for p in points):
            return
        from .modes.receiver import MAX_POINTS
        if len(points) >= MAX_POINTS:
            self.status.setText(f"At most {MAX_POINTS} channels decoded at once")
            return
        points.append({"freq": float(freq), "decoder": decoder, "muted": False})
        self._save_points(points)
        self._start_point(float(freq), decoder)
        self.status.setText(f"Decoding {freq / 1e6:.4f} MHz with {POINT_NAMES.get(decoder, decoder)} as well")

    def remove_point(self, freq):
        self._save_points([p for p in self._saved_points() if abs(p["freq"] - freq) >= 1])
        if self.running and self.mode is not None and self.mode.output == "receiver":
            self.mode.remove_point(freq)

    def remove_all_points(self):
        for p in self._saved_points():
            self.remove_point(p["freq"])

    def mute_point(self, freq, muted):
        points = self._saved_points()
        for p in points:
            if abs(p["freq"] - freq) < 1:
                p["muted"] = bool(muted)
        self._save_points(points)
        if self.running and self.mode is not None and self.mode.output == "receiver":
            self.mode.mute_point(freq, muted)

    def _show_points(self, force=False):
        """The decode points on the spectrum and in the decode area (when something about them changed)."""
        if self.mode is None or self.mode.output != "receiver":
            return
        states = self.mode.point_states() if self.running else []
        if not states and not self.running:
            states = [{"freq": p["freq"], "decoder": p["decoder"], "muted": p.get("muted", False), "heard": 0.0,
                       "in_view": False} for p in self._saved_points()]
        now = time.time()
        key = tuple((s["freq"], s["decoder"], s["muted"], s["in_view"], now - s["heard"] < 5,
                     round(s.get("afc", 0.0) / 100)) for s in states)
        if not force and key == getattr(self, "_points_key", None):
            return
        self._points_key = key
        self.receiver_panel.view.points = states
        self.decode_area.set_points(states)

    def _listen_tag(self, t):
        self._receiver_changed("freq_mhz", round(t["freq"] / 1e6, 6))
        if t.get("demod"):
            self._receiver_changed("demod", t["demod"])
        if t.get("bandwidth"):
            self._receiver_changed("bandwidth", int(t["bandwidth"]))
        if t.get("decoder") is not None and t.get("decoder") != self.mode.values.get("decoder"):
            self._receiver_changed("decoder", t["decoder"] or "")

    def decoder_settings(self):
        """The receiver's decoder's own settings (the same as on its page)."""
        M = self.decode_area.M
        if M is None or self.mode is None or self.mode.output != "receiver":
            return
        skip = {"input", "file", "device", "gain", "ppm", "freq_mhz", "file_rate", "file_offset", "bw", "squelch"} \
            if M.id == "dmr" else set()
        settings = tuple(s for s in M.settings if s.key not in skip)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{M.name} settings")
        form = SettingsForm(settings, self._values(M))
        form.changed.connect(lambda k, v: (self.settings.setValue(self._key(M, k), v),
                                           self.mode.set_decoder(M.id, self._values(M))))
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(form)
        lay = QVBoxLayout(dlg)
        lay.addWidget(area)
        dlg.resize(460, 560)
        dlg.exec()

    def decoder_click(self, hz):
        """A click on the decoder's audio waterfall: tune the decoder to that tone."""
        if self.mode is not None and self.mode.output == "receiver":
            self.mode.decoder_call(lambda d: d.set_frequency(hz))

    def _receiver_changed(self, key, value):
        """A setting changed on the receiver's toolbar (or by clicking the waterfall)."""
        if self.mode is None or self.mode.output != "receiver":
            return
        self.form.set_value(key, value)
        self.on_setting(key, value)

    def on_waterfall_click(self, f):
        if self.mode is None:
            return
        key = self.mode.set_frequency(f)
        if key:
            value = self.mode.values[key]
            self.form.set_value(key, value)
            self.on_setting(key, value)
            self.status.setText(f"Tuned to {value} Hz")

    # ---- running -------------------------------------------------------

    def on_start_toggled(self, on):
        if on:
            self._start()
        else:
            self._stop()

    def _start(self):
        if not self._start_mode():
            self.start_btn.blockSignals(True)
            self.start_btn.setChecked(False)
            self.start_btn.blockSignals(False)
            return
        if self._is_radio():
            src = "the RTL-SDR" if self.mode.values.get("input") == "rtl" else "the recording"
            self.status.setText(f"{self.mode.name}: reading {src}")
        elif getattr(self.mode, "source", "audio") == "none":
            pass                                     # no audio and no dongle (the satellite tracker)
        elif not self._start_capture():
            self._stop_mode()
            return
        self.running = True
        self.start_btn.setText("Stop")
        self.timer.start()

    def _stop(self):
        self.timer.stop()
        if self.capture:
            self.capture.stop()
            self.capture = None
        self._stop_mode()
        self.running = False
        self.update_actions()
        self.start_btn.blockSignals(True)
        self.start_btn.setChecked(False)
        self.start_btn.blockSignals(False)
        self.start_btn.setText("Start")
        self.status.setText("Stopped")

    def _start_mode(self) -> bool:
        try:
            missing = deps.missing(self.mode.requires)
            if missing:
                raise MissingParts(missing)
            self.mode.start(dict(self.mode.values))
        except MissingParts as e:
            self.offer_install(e.parts, f"{self.mode.name} needs {deps.describe(e.parts)}, which isn't installed yet.")
            self.status.setText(f"{self.mode.name}: not installed yet")
            return False
        except (RuntimeError, ValueError) as e:
            # Shown above the output (a text, table or picture mode), not as a pop-up:
            # modes start as soon as they're picked.
            self.status.setText(str(e))
            self.title.setText(f"<span style='color:#c33'>{html.escape(str(e))}</span>")
            self._warned = True
            return False
        self._mode_started = True
        self._warned = False
        self.title.setText("")
        self._setup_coverage()
        if self.mode.output == "receiver":
            self._receiver_decoder()
            for p in self._saved_points():
                self._start_point(p["freq"], p["decoder"], p.get("muted", False))
            if getattr(self.mode, "explorer", False):
                self._explorer_start()
        log.info("mode %s started", self.mode.id)
        return True

    def _stop_mode(self):
        if self.mode is not None and getattr(self, "_mode_started", False):
            if getattr(self.mode, "explorer", False):
                self._explorer_stop()
            try:
                self.handle(self.mode.stop())
                self._save_coverage()
                self.logbook.flush()
            except Exception as e:  # a decoder failing to stop shouldn't take the window down
                log.exception("stopping %s", self.mode.id)
                appreports.report_error(e, f"stopping {self.mode.id}")
            self._mode_started = False

    def _start_capture(self) -> bool:
        target = self.source_combo.currentData() or None
        self.capture = AudioCapture(target, self.mode.rate)
        try:
            self.capture.start()
        except RuntimeError as e:
            self.capture = None
            QMessageBox.warning(self, APP_NAME, str(e))
            return False
        self.status.setText(f"Listening to {self.source_combo.currentText()} for {self.mode.name}")
        return True

    def _restart_capture(self):
        if self.capture:
            self.capture.stop()
        self._start_capture()

    def _is_radio(self) -> bool:
        return getattr(self.mode, "source", "audio") == "radio"

    def update_actions(self):
        """The current mode's buttons above the output (rebuilt when they change)."""
        actions = self.mode.actions() if self.mode is not None and self.running else []
        ids = [a[0] for a in actions]
        if ids != list(self._action_buttons):
            for b in self._action_buttons.values():
                b.deleteLater()
            self._action_buttons = {}
            while self._action_layout.count():
                self._action_layout.takeAt(0)
            for action_id in ids:
                b = QPushButton()
                b.clicked.connect(lambda _=False, a=action_id: self.run_action(a))
                self._action_layout.addWidget(b)
                self._action_buttons[action_id] = b
            self._action_layout.addStretch(1)
        for action_id, label, enabled, tip in actions:
            b = self._action_buttons[action_id]
            b.setText(label)
            b.setEnabled(enabled)
            b.setToolTip(tip)
        self.action_bar.setVisible(bool(actions))

    def run_action(self, action_id):
        for key, value in self.mode.run_action(action_id).items():
            self.form.set_value(key, value)
            self.on_setting(key, value)
            self.status.setText(f"{key} set to {value}")

    def tick(self):
        audio = np.zeros(0, np.float32)
        if getattr(self.mode, "source", "audio") == "audio":
            if self.capture is None:
                return
            if self.capture.rate != self.mode.rate:
                self._restart_capture()
                return
            try:
                audio = self.capture.read()
            except RuntimeError as e:
                log.warning("capture stopped: %s", e)
                self._stop()
                QMessageBox.warning(self, APP_NAME, f"Listening stopped.\n\n{e}")
                return
        events = []
        try:
            if len(audio):
                events += self.mode.feed(audio, time.time())
            events += self.mode.poll()
        except Exception as e:  # keep listening; send the bug in
            log.exception("%s decoder failed", self.mode.id)
            appreports.report_error(e, f"decoding {self.mode.id}")
            self._stop_mode()
            self._start_mode()
            return
        self.handle(events)
        if time.time() - self._last_actions >= 1.0:
            self._last_actions = time.time()
            self.update_actions()
        if len(audio):
            self._tail = np.concatenate([self._tail, audio])[-FFT_SIZE * 2 :]
            self.waterfall.add(self._tail, self.mode.rate)
            self.waterfall.markers = self.mode.markers()
            level = 10 * np.log10(np.mean(self._tail**2) + 1e-12)
            self.level_label.setText(f"Audio {level:.0f} dBFS")

    # ---- output --------------------------------------------------------

    def handle(self, events: list[Event]):
        for e in events:
            if e.kind == "line":
                if e.style == "warn" and (self.mode.output != "text" or self._is_radio()):
                    # Warnings go above the output (table and picture modes have no text pane,
                    # and radio modes' activity line lives there).
                    self.title.setText(f"<span style='color:#c33'>{html.escape(e.text)}</span>")
                    self._warned = True
                color = {"dim": "gray", "warn": "#c33"}.get(e.style)
                text = html.escape(e.text).replace(" ", "&nbsp;") or "&nbsp;"
                self.text.appendHtml(f"<span style='color:{color}'>{text}</span>" if color else text)
                if e.style != "warn" and e.text.strip():
                    self.decoded_log.add(e.text.strip())
                    if e.style != "dim":
                        self.logbook.add(self.mode.id, e.text.strip())
            elif e.kind == "live":
                self.live.setText(e.text)
            elif e.kind == "row":
                self._add_row(e.data)
                text = "  ".join(f"{h}: {e.data[k]}" for k, h in self.mode.columns if str(e.data.get(k, "")) != "")
                self.decoded_log.add(text)
                self.logbook.add(self.mode.id, text)
            elif e.kind == "log":
                self.decoded_log.add(e.text)
                if e.style != "pos":
                    self.logbook.add(self.mode.id, e.text)
            elif e.kind == "raw":
                self.raw_log.add(e.text)
            elif e.kind == "points":
                self.map.set_points(e.data["points"], e.data.get("home"))
                self._log_points(e.data["points"], e.data.get("heard"))
            elif e.kind == "table":
                self._set_rows(e.data["rows"])
                if self._selected is not None and time.time() - self._last_details >= 1.0:
                    self.refresh_details()
            elif e.kind == "image" and e.done:
                self._last_image = e.image
                self.picture.show_image(to_qimage(e.image), e.row, getattr(self.mode, "aspect", None))
                self.title.setText(e.title)
                self.decoded_log.add(e.title)
                self.logbook.add(self.mode.id, e.title)
            elif e.kind == "image":
                self._last_image = e.image
                self.picture.show_image(to_qimage(e.image), e.row, getattr(self.mode, "aspect", None))
                self.title.setText(e.title)
            elif e.kind == "status":
                self.status.setText(e.text)
            elif e.kind == "decoded":
                text = self.decode_area.handle(e.data["event"])
                if text:
                    self.decoded_log.add(text)
                    self.logbook.add(e.data["id"], text)
            elif e.kind == "sat_catalogue":
                self.satellite_panel.set_catalogue(e.data["catalogue"])
            elif e.kind == "sat_next":
                self.satellite_panel.set_next(e.data["passes"], e.data.get("done", True))
            elif e.kind == "point_decoded" and getattr(self.mode, "explorer", False) and \
                    e.data["event"].kind in ("dsd", "explorer"):
                self._explorer_event(e.data["freq"], e.data["event"])
            elif e.kind == "point_decoded":
                d = e.data
                text = self.decode_area.point_event(d["freq"], d["id"], d["event"])
                if text:
                    self.decoded_log.add(text)
                    self.logbook.add(d["id"], text)
            elif e.kind == "decoder_audio":
                self.decode_area.audio(e.data["tail"], e.data["rate"], e.data["markers"])
            elif e.kind == "survey":
                d = e.data
                self.receiver_panel.survey_view.set_frame(d["lo"], d["hi"], d["slices"], d["res"], d["pos"], d["pass"],
                                                          d["steps"], (d["min"], d["max"]))
            elif e.kind == "identified":
                self._show_guesses(e.data)
            elif e.kind == "identifying":
                self._show_identifying(e.data)
            elif e.kind == "spectrum":
                d = e.data
                view = self.receiver_panel.view
                if self._tags_view != (d["center"], d["rate"]):
                    self._tags_view = (d["center"], d["rate"])
                    self._refresh_tags()
                view.tune, view.mode, view.bandwidth = d["tune"], d["mode"], d["bandwidth"]
                view.peak_hold = bool(self.mode.values.get("peak_hold", True))
                view.set_frame(d["db"], d["center"], d["rate"])
                self._show_points()
                self.receiver_panel.meter.setText(f"Signal {d['level']:.0f} dBFS" +
                                                  ("" if d["open"] else " · squelched"))
            elif e.kind == "activity":
                if not self._warned:
                    self.title.setText(html.escape(e.text))
            elif e.kind == "setting":
                for key, value in e.data.items():
                    self.form.set_value(key, value)
                    self.settings.setValue(self._key(self.mode, key), value)

    def _set_rows(self, rows):
        """Replace the whole table (a list that updates in place, like aircraft)."""
        t = self.table
        with filling(t):                          # re-sorted afterwards, if a column is chosen
            t.setRowCount(len(rows))
            for r, data in enumerate(rows):
                for c, (key, _) in enumerate(self.mode.columns):
                    item = t.item(r, c)
                    text = str(data.get(key, ""))
                    if item is None:
                        t.setItem(r, c, SortItem(text))
                    elif item.text() != text:
                        item.setText(text)
        # Rows are rewritten in the mode's order and then sorted: keep the selected one selected.
        col = getattr(self.mode, "key_column", "")
        c = next((i for i, (k, _) in enumerate(self.mode.columns) if k == col), None)
        if self._selected is not None and c is not None:
            for r in range(t.rowCount()):
                item = t.item(r, c)
                if item is not None and self._craft_key(item.text()) == self._selected:
                    if t.currentRow() != r:
                        sm = t.selectionModel()
                        sm.blockSignals(True)
                        t.selectRow(r)
                        sm.blockSignals(False)
                        t.viewport().update()
                    break

    def _add_row(self, data):
        t = self.table
        bar = t.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        style = data.get("_style")
        brush = {"cq": QBrush(QColor(60, 160, 90, 70)), "highlight": QBrush(QColor(255, 200, 0, 110))}.get(style)
        with filling(t):
            r = t.rowCount()
            t.insertRow(r)
            for c, (key, _) in enumerate(self.mode.columns):
                item = SortItem(str(data.get(key, "")))
                if brush:
                    item.setBackground(brush)
                if c == 0:
                    self._arrivals += 1
                    item.setData(ORDER, self._arrivals)
                t.setItem(r, c, item)
            if t.rowCount() > 5000:
                drop_oldest(t)
        if follow:
            t.scrollToBottom()

    def clear_output(self):
        self.text.clear()
        self.live.setText("")
        self.table.setRowCount(0)

    def _output_text(self) -> str:
        if self.mode.output == "table":
            rows = []
            for r in range(self.table.rowCount()):
                rows.append("\t".join(self.table.item(r, c).text() if self.table.item(r, c) else ""
                                      for c in range(self.table.columnCount())))
            return "\n".join(rows)
        return self.text.toPlainText()

    def copy_output(self):
        if self.mode.output == "image":
            if self.picture.image is not None:
                QApplication.clipboard().setImage(self.picture.image)
        else:
            QApplication.clipboard().setText(self._output_text())

    def save_output(self):
        if self.mode.output == "image":
            if self.picture.image is None:
                return
            path, _ = QFileDialog.getSaveFileName(self, "Save picture", str(Path.home() / f"{self.mode.id}.png"),
                                                  "PNG (*.png)")
            if path:
                self.picture.image.save(path, "PNG")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save decoded text", str(Path.home() / f"{self.mode.id}.txt"),
                                              "Text (*.txt)")
        if path:
            Path(path).write_text(self._output_text() + "\n", encoding="utf-8")

    def open_folder(self):
        folder = Path(self.mode.values.get("folder", Path.home())).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---- details, logbook, coverage -------------------------------------

    def _on_table_pick(self):
        col = self.mode.key_column if self.mode is not None else ""
        rows = self.table.selectionModel().selectedRows() if col else []
        if not rows:
            return
        c = next((i for i, (k, _) in enumerate(self.mode.columns) if k == col), None)
        item = self.table.item(rows[0].row(), c) if c is not None else None
        if item is not None and item.text():
            self.select_craft(item.text())

    def _craft_key(self, key) -> str:
        """The map / logbook id for a table key (ADS-B shows hex in capitals, readsb uses lower case)."""
        return str(key).lower() if self.mode.id == "adsb" else str(key)

    def select_craft(self, key):
        key = self._craft_key(key)
        self._selected = key
        self.map.select(key if self.mode.id != "ais" else int(key) if key.isdigit() else key)
        self.side.setCurrentWidget(self.details)
        self.refresh_details()

    def refresh_details(self):
        if self._selected is None or self.mode is None:
            return
        self._last_details = time.time()
        details = self.mode.details(self._selected)
        logged = self.logbook.craft_info(self.mode.id, self._selected)
        self.details.show_details(self._selected, details, logged, heard=details is not None)

    def _log_points(self, points, heard=None):
        now = time.time()
        if self.coverage is not None:
            for key, t in (heard or {}).items():
                self.coverage.heard(str(key), t)
        for p in points:
            if p.get("kind") == "landing":                 # an estimate on the map, not a craft heard
                continue
            self.logbook.see(self.mode.id, str(p["id"]), p.get("name", ""), p.get("info"), p["lat"], p["lon"],
                             alt=p.get("alt"), heading=p.get("heading"), label=p.get("label", ""), t=now)
            if self.coverage is not None:
                self.coverage.add(str(p["id"]), p.get("pos_t", now), p["lat"], p["lon"], p.get("gs"),
                                  p.get("heading"))
        if now - self._last_flush >= 2.0:
            self._last_flush = now
            self.logbook.flush()
        if self.coverage is not None and now - self._coverage_drawn >= 5.0:
            self._coverage_drawn = now
            self.map.set_rings(self.coverage.outlines())
            self.map.rings_btn.setToolTip("Range rings" + getattr(self, "_coverage_source", "") + ": " +
                                          (self.coverage.summary() or "nothing yet") +
                                          ". Green: reliable reception; red dashed: furthest position heard; "
                                          "purple: where aircraft were still heard without a position "
                                          "(estimated from their speed and track).")
            if now - self._coverage_saved >= 60.0:
                self._save_coverage()
        if self._selected is not None and now - self._last_details >= 1.0:
            self.refresh_details()

    def _setup_coverage(self):
        """ADS-B range rings around your location: from this PC's dongle, or a relay on this network
        (its own rings: another antenna). Not a relay elsewhere: the rings would be in the wrong place."""
        from .modes.radio import home
        self.coverage = None
        here = home(self.mode.values) if self.mode.id == "adsb" else None
        b = self.mode.bridge() if here is not None else None
        if here is None or not self.mode.live() or (b is not None and not b.on_lan):
            self.map.set_rings(None)
            return
        self._coverage_site = coverage.site_key(here) + (f" relay:{b.name}" if b is not None else "")
        self._coverage_source = f" (the relay {b.name})" if b is not None else ""
        data = self.logbook.load_coverage("adsb", self._coverage_site)
        self.coverage = coverage.Coverage(here, data)
        self._coverage_saved = time.time()
        self.map.set_rings(self.coverage.outlines())

    def _save_coverage(self):
        if self.coverage is not None:
            self._coverage_saved = time.time()
            site = getattr(self, "_coverage_site", None) or coverage.site_key(self.coverage.home)
            self.logbook.save_coverage("adsb", site, self.coverage.data())

    def show_logbook(self):
        if getattr(self, "_logbook_window", None) is None:
            self._logbook_window = LogbookWindow(self, self.logbook, {M.id: M.name for M in MODES})
        w = self._logbook_window
        w.show_mode(self.mode.id if self.mode is not None else "")
        w.show()
        w.raise_()
        w.activateWindow()

    # ---- relay ---------------------------------------------------------

    def relay_clicked(self):
        from . import relay
        if relay.current() is not None:
            self.relay_btn.showMenu()          # while connected the button opens its menu
            return
        dlg = RelayDialog(self, self.settings)
        if dlg.exec() and dlg.bridge is not None:
            self._relay_changed(f"Connected to the relay {dlg.bridge.name}: the RTL-SDR modes use its dongle")

    def _relay_changed(self, message, restart=True):
        from . import relay
        b = relay.current()
        self.relay_btn.setText(f"Relay: {b.name} ▾" if b else "Connect relay…")
        self.relay_btn.setToolTip(f"The relay {b.name} (version {b.relay_version or '?'})" if b else
                                  "Use an RTL-SDR on another PC running SDR Tools Relay")
        if b:
            menu = QMenu(self)
            self._relay_update_action = menu.addAction("Looking for a newer relay version…")
            self._relay_update_action.setEnabled(False)
            self._relay_update_action.triggered.connect(self.update_relay)
            menu.addAction("Check the relay's dongle…", self.check_relay_dongle)
            menu.addAction("Disconnect", self.disconnect_relay)
            self.relay_btn.setMenu(menu)
            self._run_task(relay.latest_relay_version, self._relay_latest, lambda msg: self._relay_latest(""))
        else:
            self.relay_btn.setMenu(None)
        self.status.setText(message)
        log.info(message)
        if restart and self.running and self._is_radio():
            self._stop_mode()
            self._start_mode()

    def disconnect_relay(self):
        from . import relay
        b = relay.current()
        if b is not None:
            relay.disconnect()
            self._relay_changed(f"Disconnected from the relay {b.name}: the RTL-SDR modes use this PC's dongle again")

    def check_relay_dongle(self):
        from . import relay
        b = relay.current()
        if b is None:
            return
        if self.running and self._is_radio():
            self._stop()                       # the relay has one dongle: the check needs it
        self.status.setText(f"Checking the relay {b.name}'s dongle (about 20 s)…")

        def done(report):
            lines = []
            for name, r in report.items():
                if isinstance(r, dict):
                    got = r.get("bytes")
                    verdict = (f"{got / 1e6:.1f} MB in 2 s" if got else "no samples") if got is not None else ""
                    extra = r.get("error") or r.get("skipped") or r.get("callback_error") or r.get("output", "")
                    lines.append(f"{name}: {verdict} {('— ' + extra) if extra else ''}".strip())
            text = "\n".join(lines) or json.dumps(report, indent=1)
            log.info("relay dongle check: %s", json.dumps(report))
            self.status.setText("Relay dongle check done")
            QMessageBox.information(self, "The relay's dongle", f"{b.name}:\n\n{text}\n\n(4.8 MB in 2 s is a "
                                    "dongle streaming normally at 2.4 MS/s.)")

        self._run_task(lambda: relay.diagnose(b.host, b.port, b.password), done,
                       lambda msg: (self.status.setText(f"Relay dongle check: {msg}"),
                                    QMessageBox.warning(self, "The relay's dongle", msg)))

    def _relay_latest(self, latest):
        from . import relay
        b = relay.current()
        action = getattr(self, "_relay_update_action", None)
        if b is None or action is None:
            return
        self._relay_latest_version = latest
        newer = bool(latest) and relay.parse_version(latest) > relay.parse_version(b.relay_version)
        action.setEnabled(newer and "update" in b.features)
        if not latest:
            action.setText(f"Relay version {b.relay_version or '?'} (couldn't check for a newer one)")
        elif not newer:
            action.setText(f"Relay version {b.relay_version} is the latest")
        elif "update" in b.features:
            action.setText(f"Update the relay to {latest} (it's on {b.relay_version})")
            self.status.setText(f"The relay {b.name} is on {b.relay_version}; {latest} is out: see the relay button")
        else:
            action.setText(f"Relay {latest} is out: update it on its PC (version {b.relay_version} can't be "
                           "updated from here)")
        log.info("relay %s is on %s, latest %s", b.name, b.relay_version, latest or "unknown")

    def update_relay(self):
        from . import relay
        from .relay import RelayError
        b = relay.current()
        latest = getattr(self, "_relay_latest_version", "")
        if b is None or not latest:
            return
        if QMessageBox.question(self, "Update the relay",
                                f"Update the relay {b.name} from {b.relay_version} to {latest}? It downloads the "
                                "release from GitHub, installs it and restarts, which takes a minute or two. "
                                "The running mode stops meanwhile.") != QMessageBox.StandardButton.Yes:
            return
        was_running = self.running and self._is_radio()
        if was_running:
            self._stop()
        self.status.setText(f"Asking the relay {b.name} to update to {latest}…")

        def work():
            version = relay.request_update(b.host, b.port, b.password)
            if not relay.wait_for_version(b.host, b.port, version):
                raise RelayError(f"the relay asked for {version} but hasn't come back with it after 5 minutes: "
                                 "check it on its PC")
            return version

        def done(version):
            b.relay_version = version
            self._relay_changed(f"The relay {b.name} is updated to {version}", restart=False)
            if was_running:
                self.start_btn.setChecked(True)

        def failed(msg):
            self.status.setText(f"Relay update: {msg}")
            QMessageBox.warning(self, "Update the relay", msg)
            if was_running:
                self.start_btn.setChecked(True)

        self._run_task(work, done, failed)

    def fetch_aircraft_db(self):
        """Download the aircraft database (registrations, types) in the background, then restart ADS-B."""
        if getattr(self, "_fetching_db", False):
            return
        self._fetching_db = True
        self.status.setText("Downloading the aircraft database (9 MB) for registrations and types…")

        def done(_result):
            self._fetching_db = False
            self.status.setText("Aircraft database ready: registrations, types and operators")
            if self.mode is not None and self.mode.id == "adsb" and self.running:
                self._stop_mode()
                self._start_mode()

        def failed(msg):
            self._fetching_db = False
            log.warning("aircraft database download failed: %s", msg)
            self.status.setText("Couldn't download the aircraft database; ADS-B works without it")

        self._run_task(aircraftdb.download, done, failed)

    def _show_log(self, window):
        window.show()
        window.raise_()
        window.activateWindow()

    # ---- missing parts and updates -------------------------------------

    def _run_task(self, fn, on_success, on_failure):
        task = BackgroundTask(lambda emit: fn())
        task.succeeded.connect(on_success)
        task.failed.connect(on_failure)
        task.finished.connect(lambda: self._tasks.discard(task))
        self._tasks.add(task)         # dropping a running QThread aborts the process
        task.start()

    def startup_checks(self):
        self.show_general_missing()
        if updater.due():
            updater.mark_checked()
            self._run_task(updater.check, self._on_update_checked, lambda _msg: None)

    def show_general_missing(self):
        """Offer to install whatever any mode still lacks (unless the current mode's own offer is up)."""
        if self.install_bar.isVisible() and self._to_install and self._mode_specific:
            return
        # On Windows some parts can't be had at all (readsb, GNSS-SDR): only each mode says what it lacks.
        missing = deps.missing() if deps.can_install() else []
        if missing:
            self.offer_install(missing, f"Some modes need parts that aren't installed yet: {deps.describe(missing)}.",
                               mode_specific=False)
        else:
            self.install_bar.setVisible(False)

    def _windows_parts_help(self, parts):
        """Windows: where each missing part comes from, and the folder to put programs in."""
        from . import system
        box = QMessageBox(QMessageBox.Icon.Information, "Getting the outside parts", "", parent=self)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(deps.windows_help(parts) + f"<p>The tools folder is <code>{html.escape(str(system.user_tools_dir()))}"
                    "</code>. Restart the mode (Stop, Start) once a program is in place.</p>")
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        folder = box.addButton("Open the tools folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is folder:
            system.open_folder(system.user_tools_dir())
        if not deps.missing(parts):
            self.install_bar.setVisible(False)

    def offer_install(self, parts, message, mode_specific=True):
        self._to_install = list(parts)
        self._mode_specific = mode_specific
        self.install_label.setText(message)
        self.install_bar.setVisible(True)

    def install_missing(self):
        parts = list(self._to_install)
        if not deps.can_install():
            self._windows_parts_help(parts)
            return
        accept = self.settings.value("mbelib_accepted", "false") in (True, "true")
        if "dsd-fme" in parts and not accept:
            box = QMessageBox(QMessageBox.Icon.Question, "DMR / P25 voice", deps.MBELIB_NOTICE, parent=self)
            yes = box.addButton("Build it", QMessageBox.ButtonRole.AcceptRole)
            skip = box.addButton("Skip DMR / P25", QMessageBox.ButtonRole.RejectRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is yes:
                accept = True
                self.settings.setValue("mbelib_accepted", True)
            elif box.clickedButton() is skip:
                parts.remove("dsd-fme")
            else:
                return
        if not parts:
            return
        was_running = self.running
        if was_running:
            self._stop()
        dlg = InstallDialog(self, parts, accept)
        dlg.exec()
        log.info("install finished; still missing: %s", dlg.still_missing)
        self.install_bar.setVisible(False)
        self._to_install = []
        self.help_label.setText(tuning_html(self.mode))
        self.show_general_missing()
        self.start_btn.setChecked(True)

    def check_for_updates(self):
        def done(update):
            self._on_update_checked(update)
            if update is None:
                QMessageBox.information(self, "Updates", f"{APP_NAME} {__version__} is up to date.")

        def failed(msg):
            QMessageBox.warning(self, "Updates", f"Could not check for updates:\n\n{msg}")

        self._run_task(updater.check, done, failed)

    def _on_update_checked(self, update):
        if update is not None:
            self._update = update
            self.update_btn.setText(f"Update to {update.version}")
            self.update_btn.setVisible(True)

    def install_update(self):
        update = self._update
        if update is None:
            return
        self.update_btn.setEnabled(False)
        self.status.setText(f"Installing {update.version}…")

        def failed(msg):
            self.update_btn.setEnabled(True)
            self.status.setText("Update failed")
            QMessageBox.warning(self, "Update failed", msg)

        def installed(_):
            self.settings.setValue("geometry", self.saveGeometry())
            self._stop()
            updater.restart()

        self._run_task(lambda: updater.install(update), installed, failed)

    # ---- reports -------------------------------------------------------

    def report_problem(self):
        note, ok = QInputDialog.getMultiLineText(
            self, "Report a problem",
            "What went wrong? This is sent with the app's log and the current mode's settings, "
            "never the decoded messages or pictures.",
        )
        if ok:
            extra = {"mode": self.mode.id if self.mode else "", "running": str(self.running),
                     "source": "default" if not self.source_combo.currentData() else "other output"}
            if self.mode:
                extra.update({f"setting.{k}": str(v) for k, v in self.mode.values.items()
                              if k not in ("folder", "only", "highlight")})
            appreports.send_report(note, extra, on_done=self.report_finished.emit)

    def _on_report_finished(self, sent: bool):
        if sent:
            QMessageBox.information(self, "Report a problem", "Report sent. Thanks.")
        else:
            QMessageBox.warning(self, "Report a problem", "The report could not be sent.")

    def closeEvent(self, ev):
        self.settings.setValue("geometry", self.saveGeometry())
        self._stop()
        from . import relay
        relay.disconnect()
        self.logbook.close()
        super().closeEvent(ev)


def run_gui(state_dir: Path) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    win = MainWindow(state_dir)
    win.show()
    return app.exec()
