"""Main window: mode list, waterfall, decoded output, settings and tuning help."""

import collections
import html
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
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import APP_ID, APP_NAME, __version__, appreports, deps, updater
from .capture import AudioCapture, list_outputs
from .mapview import MapView
from .modes import GROUPS, MODES
from .modes.base import Event, MissingParts, Mode, Setting

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
            w = QComboBox()
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
                     "use <b>Install</b> above the output.") + "</p>")
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
                if M.group == group:
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
        top.addWidget(self.update_btn)

        self.waterfall = Waterfall()
        self.waterfall.clicked.connect(self.on_waterfall_click)

        # Output: text, table or picture
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono)
        self.text.setMaximumBlockCount(20000)
        self.live = QLabel("")
        self.live.setFont(mono)
        self.live.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_page = QWidget()
        tl = QVBoxLayout(text_page)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.text, 1)
        tl.addWidget(self.live)
        self.table = QTableWidget(0, 0)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setFont(mono)
        self.picture = PictureView()
        self.map = MapView()
        self.table_page = QSplitter(Qt.Orientation.Vertical)
        self.table_page.addWidget(self.map)
        self.table_page.addWidget(self.table)
        self.table_page.setSizes([420, 260])
        self.output = QStackedWidget()
        self.output.addWidget(text_page)
        self.output.addWidget(self.table_page)
        self.output.addWidget(self.picture)
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
        for b in (clear_btn, copy_btn, save_btn, self.folder_btn, decoded_btn, raw_btn):
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
        self.install_btn = QPushButton("Install")
        self.install_btn.clicked.connect(self.install_missing)
        ib.addWidget(self.install_label, 1)
        ib.addWidget(self.install_btn)
        self.install_bar.setVisible(False)
        self._to_install: list[str] = []
        self._mode_specific = False
        self._tasks = set()
        self._update = None
        ol.addWidget(self.install_bar)
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
        self.status = QLabel("")
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
        self.map.set_points([], None)
        self.map.follow = True
        for w in (self.decoded_log, self.raw_log):
            w.add(f"──── {self.mode.name} ────")
        self.raw_log.setWindowTitle(f"Raw data: {self.mode.name}")
        self.decoded_log.setWindowTitle(f"Decoded log: {self.mode.name}")
        # Radio modes read the RTL-SDR or a recording, not the speakers.
        radio = self._is_radio()
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
            self.table.setColumnCount(len(m.columns))
            self.table.setHorizontalHeaderLabels([h for _, h in m.columns])
            hh = self.table.horizontalHeader()
            for i in range(len(m.columns) - 1):
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
            hh.setSectionResizeMode(len(m.columns) - 1, QHeaderView.ResizeMode.Stretch)
            self.output.setCurrentWidget(self.table_page)
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
        log.info("mode %s started", self.mode.id)
        return True

    def _stop_mode(self):
        if self.mode is not None and getattr(self, "_mode_started", False):
            try:
                self.handle(self.mode.stop())
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

    def tick(self):
        audio = np.zeros(0, np.float32)
        if not self._is_radio():
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
            elif e.kind == "live":
                self.live.setText(e.text)
            elif e.kind == "row":
                self._add_row(e.data)
                self.decoded_log.add("  ".join(f"{h}: {e.data[k]}" for k, h in self.mode.columns
                                               if str(e.data.get(k, "")) != ""))
            elif e.kind == "log":
                self.decoded_log.add(e.text)
            elif e.kind == "raw":
                self.raw_log.add(e.text)
            elif e.kind == "points":
                self.map.set_points(e.data["points"], e.data.get("home"))
            elif e.kind == "table":
                self._set_rows(e.data["rows"])
            elif e.kind == "image" and e.done:
                self._last_image = e.image
                self.picture.show_image(to_qimage(e.image), e.row, getattr(self.mode, "aspect", None))
                self.title.setText(e.title)
                self.decoded_log.add(e.title)
            elif e.kind == "image":
                self._last_image = e.image
                self.picture.show_image(to_qimage(e.image), e.row, getattr(self.mode, "aspect", None))
                self.title.setText(e.title)
            elif e.kind == "status":
                self.status.setText(e.text)
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
        t.setRowCount(len(rows))
        for r, data in enumerate(rows):
            for c, (key, _) in enumerate(self.mode.columns):
                item = t.item(r, c)
                text = str(data.get(key, ""))
                if item is None:
                    t.setItem(r, c, QTableWidgetItem(text))
                elif item.text() != text:
                    item.setText(text)

    def _add_row(self, data):
        t = self.table
        bar = t.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        r = t.rowCount()
        t.insertRow(r)
        style = data.get("_style")
        brush = {"cq": QBrush(QColor(60, 160, 90, 70)), "highlight": QBrush(QColor(255, 200, 0, 110))}.get(style)
        for c, (key, _) in enumerate(self.mode.columns):
            item = QTableWidgetItem(str(data.get(key, "")))
            if brush:
                item.setBackground(brush)
            t.setItem(r, c, item)
        if t.rowCount() > 5000:
            t.removeRow(0)
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
        missing = deps.missing()
        if missing:
            self.offer_install(missing, f"Some modes need parts that aren't installed yet: {deps.describe(missing)}.",
                               mode_specific=False)
        else:
            self.install_bar.setVisible(False)

    def offer_install(self, parts, message, mode_specific=True):
        self._to_install = list(parts)
        self._mode_specific = mode_specific
        self.install_label.setText(message)
        self.install_bar.setVisible(True)

    def install_missing(self):
        parts = list(self._to_install)
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
        super().closeEvent(ev)


def run_gui(state_dir: Path) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    win = MainWindow(state_dir)
    win.show()
    return app.exec()
