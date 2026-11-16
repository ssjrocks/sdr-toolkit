"""The HackRF bar on the SDR pages, while the relay's device is a HackRF: gain (automatic or set by hand:
LNA and VGA), the RF amp, antenna power, the baseband filter (this page's own) and the input level the
gain works from. Every change goes to the HackRF at once, on every stream from it."""

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QSizePolicy, QSpinBox, QWidget

from .hackrf_agc import TARGET

FILTERS = (1750000, 2500000, 3500000, 5000000, 5500000, 6000000, 7000000, 8000000, 9000000, 10000000,
           12000000, 14000000, 15000000, 20000000, 24000000, 28000000)


def auto_filter(rate) -> int:
    """libhackrf's automatic filter for a sample rate: the widest no more than 0.75 x the rate."""
    fits = [f for f in FILTERS if f <= 0.75 * rate]
    return fits[-1] if fits else FILTERS[0]


class LevelMeter(QWidget):
    """The input level: average power from -60 to 0 dB full scale, the target band marked; green in it,
    orange below, red when samples clip."""

    def __init__(self):
        super().__init__()
        self.level = None
        self.setMinimumSize(200, 22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_level(self, level):
        self.level = level
        self.setToolTip(self.text())
        self.update()

    def text(self) -> str:
        lv = self.level
        if not lv:
            return "Level: – (while streaming)"
        clip = lv.get("clipped", 0) * 100
        return (f"Level {lv['dbfs']:.0f} dBFS · peak {lv.get('peak', 0)}/128"
                + (f" · {clip:.2f}% clipped" if clip >= 0.005 else ""))

    def paintEvent(self, _ev):
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        pal = self.palette()
        p.setPen(QPen(pal.mid().color(), 1))
        p.setBrush(pal.base())
        p.drawRoundedRect(r, 4, 4)

        def x(db):
            return r.x() + 1 + (r.width() - 2) * max(0.0, min(1.0, (db + 60) / 60))
        band = QColor(60, 170, 90)
        band.setAlpha(40)
        p.fillRect(QRectF(x(TARGET[0]), r.y() + 1, x(TARGET[1]) - x(TARGET[0]), r.height() - 2), band)
        lv = self.level
        if lv:
            clipping = lv.get("clipped", 0) > 0.0002
            c = (QColor(210, 70, 60) if clipping else QColor(60, 170, 90) if lv["dbfs"] >= TARGET[0]
                 else QColor(230, 160, 40))
            c.setAlpha(150)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(r.x() + 1, r.y() + 1, x(lv["dbfs"]) - r.x() - 1, r.height() - 2), 3, 3)
        p.setPen(pal.text().color())
        p.drawText(r.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   p.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, int(r.width() - 12)))


class HackRFBar(QWidget):
    changed = pyqtSignal(dict)            # the settings changed here (only those)

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel("<b>HackRF</b>")
        self.gain = QComboBox()
        self.gain.addItem("Auto gain", "auto")
        self.gain.addItem("Manual gain", "manual")
        self.gain.setToolTip("Auto: the LNA and VGA follow the input level (the HackRF has no gain control of its "
                             "own), keeping it in the green band. Manual: they stay where you set them.")
        self.lna = QComboBox()
        for g in range(0, 41, 8):
            self.lna.addItem(f"LNA {g} dB", g)
        self.lna.setToolTip("RF (LNA) gain, 0-40 dB in 8 dB steps: it sets the noise figure; too much overloads "
                            "on strong signals")
        self.vga = QSpinBox()
        self.vga.setRange(0, 62)
        self.vga.setSingleStep(2)
        self.vga.setPrefix("VGA ")
        self.vga.setSuffix(" dB")
        self.vga.setToolTip("Baseband (VGA) gain, 0-62 dB in 2 dB steps")
        self.amp = QCheckBox("Amp")
        self.amp.setToolTip("RF amp: +14 dB at the antenna input, for weak signals (off for strong ones)")
        self.antenna = QCheckBox("Antenna power")
        self.antenna.setToolTip("3.3 V on the antenna port, for an active antenna or a mast LNA")
        self.filter = QComboBox()
        self.filter.setToolTip("The baseband (anti-aliasing) filter, for this page. Automatic follows the "
                               "sample rate (0.75 x); narrower cuts off what's beside the signal.")
        self.filter.addItem("Filter: auto", 0)
        for f in FILTERS:
            self.filter.addItem(f"Filter {f / 1e6:g} MHz", f)
        self.meter = LevelMeter()
        for w in (title, self.gain, self.lna, self.vga, self.amp, self.antenna, self.filter):
            lay.addWidget(w)
        lay.addWidget(self.meter, 1)
        self.gain.currentIndexChanged.connect(lambda _: self._emit("gain"))
        self.lna.currentIndexChanged.connect(lambda _: self._emit("lna"))
        self.vga.valueChanged.connect(lambda _: self._emit("vga"))
        self.amp.toggled.connect(lambda _: self._emit("amp"))
        self.antenna.toggled.connect(lambda _: self._emit("antenna"))
        self.filter.currentIndexChanged.connect(lambda _: self._emit("filter"))
        self._quiet = False

    def values(self) -> dict:
        return {"gain": self.gain.currentData(), "lna": int(self.lna.currentData()), "vga": self.vga.value() // 2 * 2,
                "amp": int(self.amp.isChecked()), "antenna": int(self.antenna.isChecked()),
                "filter": int(self.filter.currentData())}

    def set_values(self, v: dict):
        """Show these settings (without sending them back as changes)."""
        self._quiet = True
        try:
            for combo, key in ((self.gain, "gain"), (self.lna, "lna"), (self.filter, "filter")):
                if v.get(key) is not None:
                    i = combo.findData(v[key])
                    if i >= 0:
                        combo.setCurrentIndex(i)
            if v.get("vga") is not None:
                self.vga.setValue(int(v["vga"]))
            for box, key in ((self.amp, "amp"), (self.antenna, "antenna")):
                if v.get(key) is not None:
                    box.setChecked(bool(v[key]))
            manual = self.gain.currentData() == "manual"
            self.lna.setEnabled(manual)
            self.vga.setEnabled(manual)
        finally:
            self._quiet = False

    def set_rate(self, rate):
        """The automatic filter's width at the page's sample rate, in its label."""
        self.filter.setItemText(0, f"Filter: auto ({auto_filter(rate) / 1e6:g} MHz)" if rate else "Filter: auto")

    def _emit(self, key):
        if key == "gain":
            manual = self.gain.currentData() == "manual"
            self.lna.setEnabled(manual)
            self.vga.setEnabled(manual)
        if not self._quiet:
            self.changed.emit({key: self.values()[key]})
