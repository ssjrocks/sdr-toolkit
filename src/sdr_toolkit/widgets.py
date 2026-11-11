"""Widgets shared by the windows.

Labels and buttons for text of any length: they never ask for more width than the window gives
them, so a long decoded message, status or guess can't stretch the window. Text that doesn't fit
is cut with "…" (at the start for a live line, whose newest text is at the end), and all of it is
in the tooltip.

Sortable tables: click a column's heading to sort by it (again for the other way). Numbers sort as
numbers, units and all ("9 s" before "10 s", "-82.1" before "3"); the rest as text.
"""

import re
from contextlib import contextmanager

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QSizePolicy, QTableWidgetItem, QToolButton

NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
ORDER = Qt.ItemDataRole.UserRole + 1          # a row's arrival number, on its first cell


def sort_key(text):
    """Numbers (with anything after them that isn't part of a word: "45 s", "12.5 km at 30°") first,
    in order of value; then text, ignoring case; empty cells last."""
    t = text.strip()
    if not t:
        return (2, 0.0, "")
    m = NUMBER.match(t)
    if m and (m.end() == len(t) or not t[m.end()].isalnum()):     # "7C1234" is a hex id, not 7
        return (0, float(m.group()), t.lower())
    return (1, 0.0, t.lower())


class SortItem(QTableWidgetItem):
    def __lt__(self, other):
        return sort_key(self.text()) < sort_key(other.text())


def sortable(table):
    """Sort by a column when its heading is clicked; until then the rows keep their own order."""
    hh = table.horizontalHeader()
    hh.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    hh.setSortIndicatorShown(True)
    hh.setSectionsClickable(True)
    table.setSortingEnabled(True)
    return table


def unsorted(table):
    """Back to the rows' own order (new columns, say)."""
    table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)


@contextmanager
def filling(table):
    """Sorting paused while cells are written (it would move rows halfway through), then re-applied."""
    on = table.isSortingEnabled()
    table.setSortingEnabled(False)
    try:
        yield
    finally:
        if on:
            table.setSortingEnabled(True)


def drop_oldest(table):
    """Remove the row that arrived first (row 0 only while unsorted)."""
    best, row = None, 0
    for r in range(table.rowCount()):
        item = table.item(r, 0)
        n = item.data(ORDER) if item is not None else None
        if n is not None and (best is None or n < best):
            best, row = n, r
    table.removeRow(row)


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
