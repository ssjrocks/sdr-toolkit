"""Widgets shared by the windows.

Labels and buttons for text of any length: they never ask for more width than the window gives
them, so a long decoded message, status or guess can't stretch the window. Text that doesn't fit
is cut with "…" (at the start for a live line, whose newest text is at the end), and all of it is
in the tooltip.

Sortable tables: click a column's heading to sort by it (again for the other way). Numbers sort as
numbers, units and all ("9 s" before "10 s", "-82.1" before "3"); the rest as text.
"""

import functools
import re
from contextlib import contextmanager

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtWidgets import (QComboBox, QHeaderView, QLabel, QListView, QProxyStyle, QSizePolicy, QStyle,
                             QStyledItemDelegate,
                             QStyleOptionViewItem, QTableWidgetItem, QToolButton)

NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
ORDER = Qt.ItemDataRole.UserRole + 1          # a row's arrival number, on its first cell


@functools.lru_cache(maxsize=65536)
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
    """Sorting paused while cells are written (it would move rows halfway through), then re-applied.
    Columns sized to their contents are fixed meanwhile and sized once afterwards: otherwise every
    cell written re-measures its column over the rows (rows x rows: a thousand-row table took
    seconds, long enough for "not responding")."""
    on = table.isSortingEnabled()
    table.setSortingEnabled(False)
    hh = table.horizontalHeader()
    fit = [i for i in range(table.columnCount())
           if hh.sectionResizeMode(i) == QHeaderView.ResizeMode.ResizeToContents]
    for i in fit:
        hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
    table.setUpdatesEnabled(False)
    try:
        yield
    finally:
        for i in fit:
            if i < table.columnCount():
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        table.setUpdatesEnabled(True)
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


class _BoxWideList(QListView):
    """A combo's list no wider than the combo: Qt widens a popup to its longest entry, and on
    Wayland it can't tell where the window is, so a wide one ran off the window (onto the next
    monitor)."""

    def __init__(self, combo):
        super().__init__()
        self.combo = combo
        self.setWordWrap(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def sizeHintForColumn(self, column):
        return max(self.combo.width() - 2 * self.frameWidth() - self.verticalScrollBar().sizeHint().width(), 40)


class _WrapDelegate(QStyledItemDelegate):
    """Entries wrapped onto more lines to fit the combo's width, instead of widening the list."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view

    def _width(self):
        return max(self.view.sizeHintForColumn(0) - 8, 40)

    def sizeHint(self, option, index):
        fm = option.fontMetrics
        text = str(index.data() or "")
        r = fm.boundingRect(QRect(0, 0, self._width(), 10000), Qt.TextFlag.TextWordWrap, text)
        return QSize(self._width(), max(r.height(), fm.height()) + 8)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.features |= QStyleOptionViewItem.ViewItemFeature.WrapText
        style = opt.widget.style() if opt.widget is not None else self.view.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)


class _DropDownStyle(QProxyStyle):
    """Fusion opens a combo's list over the combo, as wide as its longest entry's text: this opens
    it as a plain drop-down list under the combo instead, at the combo's width."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)


_drop_down_style = None


class BoxCombo(QComboBox):
    """A combo box whose list opens at its own width, long entries wrapped: it stays inside the
    window wherever the combo is (the settings down the right side, say)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        global _drop_down_style
        if _drop_down_style is None:
            _drop_down_style = _DropDownStyle()          # kept: a widget's style isn't owned by it
        self.setStyle(_drop_down_style)
        view = _BoxWideList(self)
        self.setView(view)
        view.setItemDelegate(_WrapDelegate(view))
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)

    def showPopup(self):
        self.view().doItemsLayout()                  # the wrapping depends on the combo's width now
        super().showPopup()
