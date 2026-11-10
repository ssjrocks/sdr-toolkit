"""The receiver's screen: a toolbar, the spectrum and the waterfall."""

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QCheckBox,
    QSplitter,
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

BAR = 16                         # px: the grab bar along the top (drag to slide the band)
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
    slid = pyqtSignal(float)           # the grab bar dragged: the dongle's new centre (Hz)
    menu = pyqtSignal(float, object)   # right-click without dragging: (Hz there, global position)

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
        self.tags: list[dict] = []      # {"freq", "label"} shown as markers
        self._slide_center = None       # where the band is being slid to, drawn before it arrives

    # ---- data ----------------------------------------------------------

    def set_frame(self, db, center, rate):
        if self._slide_center is not None and self._drag is None and abs(center - self._slide_center) < 1:
            self._slide_center = None           # the dongle has arrived where the bar was slid to
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
        mid = (self._slide_center if self._slide_center is not None else self.center) + self.view_center
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
        # The grab bar: drag it to slide the band the dongle sees.
        p.fillRect(QRectF(0, 0, w, BAR), QColor(45, 55, 80))
        p.setPen(QColor(190, 200, 225))
        p.drawText(QRectF(0, 0, w, BAR), Qt.AlignmentFlag.AlignCenter,
                   "◀  drag to slide along the band  ▶" if self._slide_center is None else
                   f"centre {self._slide_center / 1e6:.4f} MHz")
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
        # Tags
        for t in self.tags:
            if lo <= t["freq"] <= hi:
                xt = self.x_of(t["freq"])
                pen = QPen(QColor(255, 210, 60, 200), 1)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(QPointF(xt, BAR), QPointF(xt, top))
                label = t["label"]
                r = p.fontMetrics().boundingRect(label)
                box = QRectF(xt + 2, BAR + 2, r.width() + 8, r.height() + 2)
                p.fillRect(box, QColor(255, 210, 60, 220))
                p.setPen(QColor(20, 20, 20))
                p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        # Tuned frequency
        p.setPen(QPen(QColor(255, 70, 70), 1))
        xt = self.x_of(self.tune)
        p.drawLine(QPointF(xt, 0), QPointF(xt, h))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QPointF(xt + 4, BAR + 30), f"{self.tune / 1e6:.6f} MHz {self.mode}")
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
        pos = ev.position()
        if ev.button() == Qt.MouseButton.RightButton:
            self._drag = ("pan", pos.x(), self.view_center, pos)
        elif pos.y() < BAR:
            self._drag = ("slide", pos.x(), self.center)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        else:
            self._drag = ("tune",)
            self.tuned.emit(self.hz_of(pos.x()))

    def mouseMoveEvent(self, ev):
        self._hover = ev.position()
        if self._drag and self._drag[0] == "pan":
            _, x0, vc0, _ = self._drag
            self.view_center = vc0 - (ev.position().x() - x0) / max(self.width(), 1) * self.rate * self.zoom * 2
            self._limit()
        elif self._drag and self._drag[0] == "slide":
            _, x0, c0 = self._drag
            # Dragging the band right shows lower frequencies, as dragging a map does.
            self._slide_center = c0 - (ev.position().x() - x0) / max(self.width(), 1) * self.rate * self.zoom * 2
        elif self._drag:
            self.tuned.emit(self.hz_of(ev.position().x()))
        elif ev.position().y() < BAR:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, ev):
        drag, self._drag = self._drag, None
        if drag and drag[0] == "slide" and self._slide_center is not None:
            self.slid.emit(self._slide_center)
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif drag and drag[0] == "pan" and (ev.position() - drag[3]).manhattanLength() < 5:
            self.menu.emit(self.hz_of(ev.position().x()), ev.globalPosition().toPoint())
        self._slide_center = None if not (drag and drag[0] == "slide") else self._slide_center

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
    identify = pyqtSignal()               # Identify pressed
    guesses = pyqtSignal(object)          # the identification line clicked (button, for its menu)
    tag = pyqtSignal()                    # Tag pressed (the tuned frequency)
    survey = pyqtSignal(bool)             # Survey toggled

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
        from .decode import DECODERS
        self.identify_btn = QToolButton()
        self.identify_btn.setText("Identify")
        self.identify_btn.setToolTip("Guess once what the signal you're tuned to is, and offer what to do with it. "
                                     "Guessing by itself every few seconds is the Identify automatically setting.")
        self.identify_btn.clicked.connect(self.identify.emit)
        self.looks = QToolButton()
        self.looks.setText("Looks like: …")
        self.looks.setToolTip("The best guess for the tuned signal. Click for all guesses and what to do.")
        self.looks.setAutoRaise(True)
        self.looks.clicked.connect(lambda: self.guesses.emit(self.looks))
        self.tag_btn = QToolButton()
        self.tag_btn.setText("Tag…")
        self.tag_btn.setToolTip("Tag the tuned frequency (shown on the spectrum; right-click the spectrum for more)")
        self.tag_btn.clicked.connect(self.tag.emit)
        self.decoder = QComboBox()
        self.decoder.addItem("Nothing", "")
        names = {"morse": "Morse", "rtty": "RTTY", "psk": "PSK31", "navtex": "NAVTEX", "ft8": "FT8", "ft4": "FT4",
                 "sstv": "SSTV", "wefax": "Weather fax", "aprs": "APRS", "pager": "Pagers", "tones": "DTMF/CTCSS",
                 "dmr": "DMR/P25"}
        for k in DECODERS:
            self.decoder.addItem(names.get(k, k), k)
        self.decoder.setToolTip("Run a decoder on what you're hearing (it also sets the mode it wants)")
        self.decoder.currentIndexChanged.connect(lambda _: self._emit("decoder", self.decoder.currentData()))
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.addWidget(self.identify_btn)
        row2.addWidget(self.looks, 1)
        row2.addWidget(self.tag_btn)
        row2.addWidget(QLabel("Decode"))
        row2.addWidget(self.decoder)
        self.survey_btn = QToolButton()
        self.survey_btn.setText("Survey")
        self.survey_btn.setCheckable(True)
        self.survey_btn.setToolTip("Sweep the dongle across its whole range and stitch it into one waterfall "
                                   "(the audio pauses). Zoom in to sweep less, faster.")
        self.survey_btn.toggled.connect(self._survey_toggled)
        row2.addWidget(self.survey_btn)
        self.dwell_label = QLabel()
        self.dwell_label.setMinimumWidth(95)
        self.dwell = QSlider(Qt.Orientation.Horizontal)
        self.dwell.setRange(0, 200)                 # tens of ms
        self.dwell.setMinimumWidth(80)
        self.dwell.setMaximumWidth(160)
        self.dwell.setToolTip("Dwell: how long the survey measures each slice, keeping the strongest moment. "
                              "Longer catches signals that are only on now and then; passes take longer.")
        self.dwell_label.setToolTip(self.dwell.toolTip())
        row2.addWidget(self.dwell_label)
        row2.addWidget(self.dwell)
        self._show_dwell(0)
        self.dwell_label.hide()
        self.dwell.hide()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addLayout(row2)
        from PyQt6.QtWidgets import QStackedWidget
        self.survey_view = SurveyView()
        self.pages = QStackedWidget()
        self.pages.addWidget(self.view)
        self.pages.addWidget(self.survey_view)
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(self.pages)
        lay.addWidget(self.split, 1)
        self._quiet = False
        self.freq.valueChanged.connect(lambda v: self._emit("freq_mhz", round(v, 6)))
        self.step.currentIndexChanged.connect(self._step_changed)
        self.mode.currentIndexChanged.connect(lambda _: self._emit("demod", self.mode.currentData()))
        self.bw.valueChanged.connect(lambda v: self._emit("bandwidth", int(v)))
        self.sq_on.toggled.connect(lambda v: self._emit("squelch_on", bool(v)))
        self.sq.valueChanged.connect(lambda v: self._emit("squelch", int(v)))
        self.vol.valueChanged.connect(lambda v: self._emit("volume", int(v)))
        self.mute.toggled.connect(lambda v: self._emit("mute", bool(v)))
        self.dwell.valueChanged.connect(self._dwell_moved)
        self.view.tuned.connect(self._clicked)

    def _survey_toggled(self, on):
        self.pages.setCurrentWidget(self.survey_view if on else self.view)
        self.dwell_label.setVisible(on)
        self.dwell.setVisible(on)
        self.survey.emit(on)

    def _show_dwell(self, ms):
        self.dwell_label.setText(f"Dwell {ms} ms" if ms < 1000 else f"Dwell {ms / 1000:.2f} s")

    def _dwell_moved(self, v):
        self._show_dwell(v * 10)
        self._emit("dwell_ms", v * 10)

    def add_decode_area(self, widget):
        """The decoded output, under the spectrum (the window builds it)."""
        self.split.addWidget(widget)
        self.split.setSizes([500, 300])

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
            self.dwell.setValue(int(v.get("dwell_ms", 0)) // 10)
            self._show_dwell(self.dwell.value() * 10)
            self.decoder.setCurrentIndex(max(self.decoder.findData(v.get("decoder", "")), 0))
        finally:
            self._quiet = False


class SurveyView(QWidget):
    """The survey: a stitched spectrum of a wide range over a waterfall of its passes."""

    view_changed = pyqtSignal(float, float)     # the range shown (Hz): sweep only that
    listen = pyqtSignal(float)                  # double-click: go back to the receiver, tuned there

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(280)
        self.setMouseTracking(True)
        self.setToolTip("Wheel: zoom (sweeps only what's on screen). Drag: pan. Double-click: listen there.")
        self.cmap = _colour_map()
        self.lo, self.hi = 24e6, 1766e6            # what's on screen
        self.full = (24e6, 1766e6)
        self.db = None
        self.pos = None
        self.pass_time = 0.0
        self.steps = 0
        self.lo_db, self.hi_db = -100.0, -30.0
        self._rows = None
        self._last_row = 0.0
        self._drag = None
        self._hover = None
        from PyQt6.QtCore import QTimer
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(lambda: self.view_changed.emit(self.lo, self.hi))

    def set_frame(self, lo, hi, db, pos, pass_time, steps, full):
        import time as _time
        if self.db is None:
            self.lo, self.hi = lo, hi
        self.full = full
        if abs(lo - self.lo) > 1 or abs(hi - self.hi) > 1:
            return                                  # an old range's frame, from before a zoom or pan
        self.db, self.pos, self.pass_time, self.steps = db, pos, pass_time, steps
        have = db[np.isfinite(db)]
        if len(have):
            floor, top = float(np.percentile(have, 20)), float(np.max(have))
            self.lo_db += 0.2 * ((floor - 10) - self.lo_db)
            self.hi_db += 0.2 * (max(top + 5, floor + 30) - self.hi_db)
        if self._rows is None or self._rows.shape[1] != len(db):
            self._rows = np.zeros((ROWS, len(db)), np.uint8)
        now = _time.monotonic()
        if now - self._last_row >= 0.5:
            self._last_row = now
            idx = np.clip((np.nan_to_num(db, nan=self.lo_db) - self.lo_db) / max(self.hi_db - self.lo_db, 1) * 255,
                          0, 255).astype(np.uint8)
            self._rows = np.roll(self._rows, 1, axis=0)
            self._rows[0] = idx
        self.update()

    def x_of(self, hz):
        return (hz - self.lo) / (self.hi - self.lo) * self.width()

    def hz_of(self, x):
        return self.lo + x / max(self.width(), 1) * (self.hi - self.lo)

    def paintEvent(self, _ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        top = int(h * 0.4)
        p.fillRect(self.rect(), QColor(10, 12, 20))
        head = (f"Survey {self.lo / 1e6:.1f}–{self.hi / 1e6:.1f} MHz · {self.steps} hop{'s' if self.steps != 1 else ''}"
                + (f" · a pass takes {self.pass_time:.1f} s" if self.pass_time else "") + " · double-click to listen")
        p.setPen(QColor(200, 210, 230))
        p.drawText(QPointF(6, 14), head)
        if self.db is None:
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Surveying…")
            p.end()
            return
        if self._rows is not None:
            img = np.ascontiguousarray(self.cmap[self._rows])
            qimg = QImage(img.data, img.shape[1], img.shape[0], img.shape[1] * 4, QImage.Format.Format_RGB32)
            p.drawImage(QRectF(0, top, w, h - top), qimg)
        p.setPen(QPen(QColor(60, 70, 90), 1))
        step = SpectrumView._grid_step(self.hi - self.lo)
        f = np.ceil(self.lo / step) * step
        while f < self.hi:
            x = self.x_of(f)
            p.drawLine(QPointF(x, 20), QPointF(x, top))
            p.setPen(QColor(150, 160, 180))
            p.drawText(QPointF(x + 3, top - 4), fmt_freq(f))
            p.setPen(QPen(QColor(60, 70, 90), 1))
            f += step
        xs = np.linspace(0, w, len(self.db))
        p.setPen(QPen(QColor(120, 220, 255), 1))
        pts, run = [], []
        for x, v in zip(xs, self.db):
            if np.isfinite(v):
                run.append(QPointF(x, top - (v - self.lo_db) / max(self.hi_db - self.lo_db, 1) * (top - 24)))
            elif run:
                pts.append(run)
                run = []
        if run:
            pts.append(run)
        for r in pts:
            p.drawPolyline(QPolygonF(r))
        if self.pos is not None:
            xp = self.x_of(self.pos)
            p.setPen(QPen(QColor(255, 200, 60, 160), 2))
            p.drawLine(QPointF(xp, 20), QPointF(xp, top))
        if self._hover is not None:
            p.setPen(QColor(220, 220, 220))
            p.drawText(QPointF(min(self._hover.x() + 8, w - 110), max(self._hover.y() - 6, 30)),
                       f"{self.hz_of(self._hover.x()) / 1e6:.3f} MHz")
        p.end()

    def _moved(self):
        lo_min, hi_max = self.full
        span = min(self.hi - self.lo, hi_max - lo_min)
        self.lo = float(np.clip(self.lo, lo_min, hi_max - span))
        self.hi = self.lo + span
        self._rows = None                       # a new range: start its waterfall afresh
        self.db = None if self.db is None else np.full_like(self.db, np.nan)
        self.update()
        self._debounce.start()

    def wheelEvent(self, ev):
        at = self.hz_of(ev.position().x())
        frac = ev.position().x() / max(self.width(), 1)
        span = (self.hi - self.lo) * (0.7 if ev.angleDelta().y() > 0 else 1.4)
        span = float(np.clip(span, 1e6, self.full[1] - self.full[0]))
        self.lo = at - frac * span
        self.hi = self.lo + span
        self._moved()

    def mousePressEvent(self, ev):
        self._drag = (ev.position().x(), self.lo)

    def mouseMoveEvent(self, ev):
        self._hover = ev.position()
        if self._drag is not None:
            x0, lo0 = self._drag
            span = self.hi - self.lo
            self.lo = lo0 - (ev.position().x() - x0) / max(self.width(), 1) * span
            self.hi = self.lo + span
            lo_min, hi_max = self.full
            self.lo = float(np.clip(self.lo, lo_min, hi_max - span))
            self.hi = self.lo + span
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._drag is not None and abs(ev.position().x() - self._drag[0]) > 3:
            self._moved()
        self._drag = None

    def mouseDoubleClickEvent(self, ev):
        self.listen.emit(self.hz_of(ev.position().x()))
