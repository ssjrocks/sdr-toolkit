"""The receiver's screen: a toolbar, the spectrum and the waterfall."""

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .dsp import MODES

STEPS = ((100, "100 Hz"), (1000, "1 kHz"), (5000, "5 kHz"), (6250, "6.25 kHz"), (10000, "10 kHz"),
         (12500, "12.5 kHz"), (25000, "25 kHz"), (100000, "100 kHz"), (200000, "200 kHz"), (1000000, "1 MHz"))
ROWS = 400


def _colour_map():
    stops = np.array([[0, 0, 0], [0, 0, 120], [0, 90, 220], [0, 200, 200], [240, 230, 0], [255, 60, 0],
                      [255, 255, 255]], float)
    x = np.linspace(0, len(stops) - 1, 256)
    i = np.minimum(x.astype(int), len(stops) - 2)
    f = (x - i)[:, None]
    rgb = ((1 - f) * stops[i] + f * stops[i + 1]).astype(np.uint32)
    return (0xFF000000 | (rgb[:, 0] << 16) | (rgb[:, 1] << 8) | rgb[:, 2]).astype(np.uint32)


def fmt_freq(hz: float) -> str:
    if abs(hz) >= 1e6:
        return f"{hz / 1e6:.3f} MHz".replace(".000 MHz", " MHz")
    return f"{hz / 1e3:.1f} kHz"


class SpectrumView(QWidget):
    tuned = pyqtSignal(float)          # a frequency clicked or dragged to (Hz)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(280)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Click or drag to tune. Wheel: zoom. Right-drag: pan. Double-click: whole span.")
        self.cmap = _colour_map()
        self.center = 100e6
        self.rate = 2.4e6
        self.tune = 100e6
        self.mode = "WFM"
        self.bandwidth = 180000.0
        self.db = None
        self.peak = None
        self.peak_hold = True
        self.lo_db, self.hi_db = -110.0, -20.0
        self.auto_range = True
        self.zoom = 1.0                 # visible fraction of the span
        self.view_center = 0.0          # offset from the dongle's centre (Hz) at the middle of the view
        self._rows = None               # waterfall: colour indices, newest first
        self._drag = None
        self._hover = None

    # ---- data ----------------------------------------------------------

    def set_frame(self, db, center, rate):
        if self.center != center or self.rate != rate or self._rows is None or self._rows.shape[1] != len(db):
            if self.rate != rate or self._rows is None or self._rows.shape[1] != len(db):
                self.view_center, self.zoom = 0.0, 1.0
            self._rows = np.zeros((ROWS, len(db)), np.uint8)
            self.peak = None
        self.center, self.rate = float(center), float(rate)
        self.db = db
        self.peak = db.copy() if self.peak is None else np.maximum(self.peak - 0.15, db)
        if self.auto_range:
            floor = float(np.percentile(db, 20))
            top = float(np.max(db))
            self.lo_db += 0.1 * ((floor - 10) - self.lo_db)
            self.hi_db += 0.1 * (max(top + 5, floor + 30) - self.hi_db)
        idx = np.clip((db - self.lo_db) / max(self.hi_db - self.lo_db, 1) * 255, 0, 255).astype(np.uint8)
        self._rows = np.roll(self._rows, 1, axis=0)
        self._rows[0] = idx
        self.update()

    # ---- geometry --------------------------------------------------------

    def span(self):
        half = self.rate / 2 * self.zoom
        mid = self.center + self.view_center
        return mid - half, mid + half

    def x_of(self, hz):
        lo, hi = self.span()
        return (hz - lo) / (hi - lo) * self.width()

    def hz_of(self, x):
        lo, hi = self.span()
        return lo + x / max(self.width(), 1) * (hi - lo)

    def passband(self):
        bw = self.bandwidth
        if self.mode == "USB":
            return self.tune + 100, self.tune + 100 + bw
        if self.mode == "LSB":
            return self.tune - 100 - bw, self.tune - 100
        return self.tune - bw / 2, self.tune + bw / 2

    # ---- painting ----------------------------------------------------------

    def paintEvent(self, _ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        top = int(h * 0.38)
        p.fillRect(self.rect(), QColor(10, 12, 20))
        if self.db is None:
            p.setPen(QColor(180, 180, 180))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for samples…")
            p.end()
            return
        n = len(self.db)
        lo, hi = self.span()
        b0 = int(np.clip(((lo - self.center) / self.rate + 0.5) * n, 0, n - 1))
        b1 = int(np.clip(((hi - self.center) / self.rate + 0.5) * n, b0 + 1, n))
        # Waterfall
        rows = self._rows[:, b0:b1]
        img = np.ascontiguousarray(self.cmap[rows])
        qimg = QImage(img.data, img.shape[1], img.shape[0], img.shape[1] * 4, QImage.Format.Format_RGB32)
        p.drawImage(QRectF(0, top, w, h - top), qimg)
        # Grid and frequency scale
        p.setPen(QPen(QColor(60, 70, 90), 1))
        step = self._grid_step(hi - lo)
        f = np.ceil(lo / step) * step
        while f < hi:
            x = self.x_of(f)
            p.drawLine(QPointF(x, 0), QPointF(x, top))
            p.setPen(QColor(150, 160, 180))
            p.drawText(QPointF(x + 3, top - 4), fmt_freq(f))
            p.setPen(QPen(QColor(60, 70, 90), 1))
            f += step
        for db in range(int(self.lo_db // 10 * 10), int(self.hi_db) + 1, 10):
            y = self._y(db, top)
            p.drawLine(QPointF(0, y), QPointF(w, y))
            p.setPen(QColor(120, 130, 150))
            p.drawText(QPointF(3, y - 2), f"{db}")
            p.setPen(QPen(QColor(60, 70, 90), 1))
        # Passband
        a, b = self.passband()
        xa, xb = self.x_of(a), self.x_of(b)
        p.fillRect(QRectF(xa, 0, max(xb - xa, 1), h), QColor(255, 255, 255, 28))
        # Spectrum line and peak hold
        xs = np.linspace(0, w, b1 - b0)
        if self.peak_hold and self.peak is not None:
            p.setPen(QPen(QColor(200, 120, 40, 140), 1))
            p.drawPolyline(QPolygonF([QPointF(x, self._y(v, top)) for x, v in zip(xs, self.peak[b0:b1])]))
        p.setPen(QPen(QColor(120, 220, 255), 1))
        p.drawPolyline(QPolygonF([QPointF(x, self._y(v, top)) for x, v in zip(xs, self.db[b0:b1])]))
        # Tuned frequency
        p.setPen(QPen(QColor(255, 70, 70), 1))
        xt = self.x_of(self.tune)
        p.drawLine(QPointF(xt, 0), QPointF(xt, h))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QPointF(xt + 4, 14), f"{self.tune / 1e6:.6f} MHz {self.mode}")
        if self._hover is not None:
            p.setPen(QColor(200, 200, 200))
            p.drawText(QPointF(min(self._hover.x() + 8, w - 110), self._hover.y() - 6),
                       f"{self.hz_of(self._hover.x()) / 1e6:.4f} MHz")
        p.end()

    def _y(self, db, top):
        return top - (db - self.lo_db) / max(self.hi_db - self.lo_db, 1) * (top - 18)

    @staticmethod
    def _grid_step(span):
        for s in (1e3, 2e3, 5e3, 10e3, 20e3, 25e3, 50e3, 100e3, 200e3, 250e3, 500e3, 1e6, 2e6):
            if span / s <= 10:
                return s
        return 5e6

    # ---- mouse -----------------------------------------------------------

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self._drag = ("pan", ev.position().x(), self.view_center)
        else:
            self._drag = ("tune",)
            self.tuned.emit(self.hz_of(ev.position().x()))

    def mouseMoveEvent(self, ev):
        self._hover = ev.position()
        if self._drag and self._drag[0] == "pan":
            _, x0, vc0 = self._drag
            self.view_center = vc0 - (ev.position().x() - x0) / max(self.width(), 1) * self.rate * self.zoom * 2
            self._limit()
        elif self._drag:
            self.tuned.emit(self.hz_of(ev.position().x()))
        self.update()

    def mouseReleaseEvent(self, _ev):
        self._drag = None

    def leaveEvent(self, _ev):
        self._hover = None
        self.update()

    def mouseDoubleClickEvent(self, _ev):
        self.zoom, self.view_center = 1.0, 0.0
        self.update()

    def wheelEvent(self, ev):
        at = self.hz_of(ev.position().x())
        z = self.zoom * (0.8 if ev.angleDelta().y() > 0 else 1.25)
        self.zoom = float(np.clip(z, 0.01, 1.0))
        # keep the frequency under the cursor where it is
        frac = ev.position().x() / max(self.width(), 1)
        self.view_center = at - self.center - (frac - 0.5) * self.rate * self.zoom
        self._limit()
        self.update()

    def _limit(self):
        room = self.rate / 2 * (1 - self.zoom)
        self.view_center = float(np.clip(self.view_center, -room, room))


class ReceiverPanel(QWidget):
    """The toolbar above the spectrum. changed(key, value) is a setting the window saves and applies."""

    changed = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.view = SpectrumView()
        self.freq = QDoubleSpinBox()
        self.freq.setDecimals(6)
        self.freq.setRange(0.0, 2200.0)
        self.freq.setSuffix(" MHz")
        self.freq.setKeyboardTracking(False)
        self.freq.setSingleStep(0.0125)
        self.freq.setToolTip("The frequency you're listening to. Arrow keys / wheel move by the step.")
        f = self.freq.font()
        f.setPointSizeF(f.pointSizeF() * 1.4)
        f.setBold(True)
        self.freq.setFont(f)
        self.freq.setMinimumWidth(200)
        self.step = QComboBox()
        for hz, text in STEPS:
            self.step.addItem(text, hz)
        self.step.setToolTip("Tuning step for the arrows and wheel on the frequency")
        self.mode = QComboBox()
        for m, (_, _, _, help_) in MODES.items():
            self.mode.addItem(m, m)
            self.mode.setItemData(self.mode.count() - 1, help_, Qt.ItemDataRole.ToolTipRole)
        self.bw = QSpinBox()
        self.bw.setRange(50, 220000)
        self.bw.setSingleStep(500)
        self.bw.setSuffix(" Hz")
        self.bw.setKeyboardTracking(False)
        self.bw.setToolTip("Bandwidth of the demodulator's filter")
        self.sq_on = QCheckBox("Squelch")
        self.sq = QSlider(Qt.Orientation.Horizontal)
        self.sq.setRange(-120, 0)
        self.sq.setMinimumWidth(80)
        self.sq.setMaximumWidth(130)
        self.sq.setToolTip("Mute the audio while the channel is weaker than this (dBFS)")
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 200)
        self.vol.setMinimumWidth(80)
        self.vol.setMaximumWidth(130)
        self.vol.setToolTip("Volume")
        self.mute = QToolButton()
        self.mute.setCheckable(True)
        self.mute.setText("Mute")
        self.meter = QLabel("—")
        self.meter.setMinimumWidth(170)
        self.meter.setToolTip("Signal in the channel (dBFS), and whether the squelch is open")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for wdg in (self.freq, self.step, self.mode, self.bw, self.sq_on, self.sq, QLabel("Vol"), self.vol, self.mute):
            row.addWidget(wdg)
        row.addStretch(1)
        row.addWidget(self.meter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.view, 1)
        self._quiet = False
        self.freq.valueChanged.connect(lambda v: self._emit("freq_mhz", round(v, 6)))
        self.step.currentIndexChanged.connect(self._step_changed)
        self.mode.currentIndexChanged.connect(lambda _: self._emit("demod", self.mode.currentData()))
        self.bw.valueChanged.connect(lambda v: self._emit("bandwidth", int(v)))
        self.sq_on.toggled.connect(lambda v: self._emit("squelch_on", bool(v)))
        self.sq.valueChanged.connect(lambda v: self._emit("squelch", int(v)))
        self.vol.valueChanged.connect(lambda v: self._emit("volume", int(v)))
        self.mute.toggled.connect(lambda v: self._emit("mute", bool(v)))
        self.view.tuned.connect(self._clicked)

    def _emit(self, key, value):
        if not self._quiet:
            self.changed.emit(key, value)

    def _step_changed(self, _):
        hz = self.step.currentData()
        self.freq.setSingleStep(hz / 1e6)
        self._emit("step", int(hz))

    def _clicked(self, hz):
        step = self.step.currentData() or 1
        self.changed.emit("freq_mhz", round(round(hz / step) * step / 1e6, 6))

    def show_values(self, v: dict):
        """Put the settings on the toolbar without sending them back."""
        self._quiet = True
        try:
            self.freq.setValue(float(v.get("freq_mhz", 100.0)))
            i = self.step.findData(int(v.get("step", 100000)))
            self.step.setCurrentIndex(max(i, 0))
            self.freq.setSingleStep((self.step.currentData() or 100000) / 1e6)
            self.mode.setCurrentIndex(max(self.mode.findData(v.get("demod", "WFM")), 0))
            lo_bw, hi_bw = MODES[v.get("demod", "WFM")][1:3]
            self.bw.setRange(lo_bw, hi_bw)
            self.bw.setValue(int(v.get("bandwidth", MODES[v.get("demod", "WFM")][0])))
            self.sq_on.setChecked(bool(v.get("squelch_on")))
            self.sq.setValue(int(v.get("squelch", -60)))
            self.vol.setValue(int(v.get("volume", 70)))
            self.mute.setChecked(bool(v.get("mute")))
        finally:
            self._quiet = False
