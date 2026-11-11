"""Labels and buttons for text of any length: they never ask for more width than the window gives
them, so a long decoded message, status or guess can't stretch the window. Text that doesn't fit
is cut with "…" (at the start for a live line, whose newest text is at the end), and all of it is
in the tooltip."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QSizePolicy, QToolButton


class _Fits:
    def _setup(self, keep_end):
        self._full = ""
        self._elide = Qt.TextElideMode.ElideLeft if keep_end else Qt.TextElideMode.ElideRight
        self.setSizePolicy(QSizePolicy.Policy.Ignored, self.sizePolicy().verticalPolicy())
        self.setMinimumWidth(24)

    def setText(self, text):
        self._full = text or ""
        self._fit()

    def text(self):
        return self._full

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def _room(self):
        return self.width()

    def _fit(self):
        one_line = " ".join(self._full.split("\n"))
        shown = self.fontMetrics().elidedText(one_line, self._elide, max(self._room(), 0))
        self._base.setText(self, shown)
        self._base.setToolTip(self, self._full if shown != one_line else self._tip)

    def setToolTip(self, tip):
        self._tip = tip
        self._fit()


class FitLabel(_Fits, QLabel):
    _base = QLabel

    def __init__(self, text="", keep_end=False):
        QLabel.__init__(self)
        self._tip = ""
        self._setup(keep_end)
        self.setText(text)

    def _room(self):
        return self.contentsRect().width() - 2


class FitButton(_Fits, QToolButton):
    _base = QToolButton

    def __init__(self, text=""):
        QToolButton.__init__(self)
        self._tip = ""
        self._setup(False)
        self.setText(text)

    def _room(self):
        return self.width() - 12
