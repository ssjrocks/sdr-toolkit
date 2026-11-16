"""The SDR pages' link meters, in the top bar: the device's USB link (a HackRF or an RTL-SDR, on this PC
or a relay's) and the network link to a relay. Each is a bar of what's flowing against what the
sample rate needs (2 bytes a sample), with the rate, and for the network the round trip (ping) and
the buffers the relay had to drop."""

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget


def rate_text(bps) -> str:
    if bps is None:
        return "–"
    if bps >= 1e6:
        return f"{bps / 1e6:.1f} MB/s"
    if bps >= 1e3:
        return f"{bps / 1e3:.0f} kB/s"
    return f"{bps:.0f} B/s"


class LinkMeter(QWidget):
    """One link: a title, a bar (value against the most it should carry) and the figures."""

    def __init__(self, title):
        super().__init__()
        self.title = title
        self.value = None                 # bytes/s flowing, None: not known
        self.needed = None                # bytes/s the sample rate needs (the bar's full width)
        self.extra = ""                   # "ping 1.2 ms · 0 dropped"...
        self.state = "idle"               # idle, good, short (below what's needed), bad (dropping)
        self.setMinimumWidth(220)
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def show_link(self, value, needed, extra="", state=None):
        self.value, self.needed, self.extra = value, needed, extra
        if state is None:
            state = ("idle" if value is None or not needed else
                     "good" if value >= 0.95 * needed else "short")
        self.state = state
        self.setToolTip(self.text())
        self.update()

    def text(self) -> str:
        if self.value is None:
            return f"{self.title}: {self.extra or 'not in use'}"
        of = f" of {rate_text(self.needed)}" if self.needed else ""
        return f"{self.title}: {rate_text(self.value)}{of}" + (f" · {self.extra}" if self.extra else "")

    def paintEvent(self, _ev):
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        pal = self.palette()
        p.setPen(QPen(pal.mid().color(), 1))
        p.setBrush(pal.base())
        p.drawRoundedRect(r, 4, 4)
        if self.value is not None and self.needed:
            frac = max(0.0, min(1.0, self.value / self.needed))
            colour = {"good": QColor(60, 170, 90), "short": QColor(230, 160, 40), "bad": QColor(210, 70, 60)}.get(
                self.state, QColor(140, 140, 140))
            colour.setAlpha(150)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            p.drawRoundedRect(QRectF(r.x() + 1, r.y() + 1, (r.width() - 2) * frac, r.height() - 2), 3, 3)
        p.setPen(pal.text().color())
        p.drawText(r.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   p.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, int(r.width() - 12)))


class LinksBar(QWidget):
    """The two meters side by side."""

    def __init__(self):
        super().__init__()
        self.usb = LinkMeter("USB link")
        self.net = LinkMeter("Relay network link")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.usb, 1)
        lay.addWidget(self.net, 1)
