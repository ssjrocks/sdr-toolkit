"""The DMR / P25 explorer's lists: channels, systems, talkgroups, radios and what happened."""

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .widgets import FitLabel, SortItem, filling, sortable


def ago(t, now=None) -> str:
    if not t:
        return ""
    s = int((now or time.time()) - t)
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else time.strftime("%d %b %H:%M", time.localtime(t))


def mhz(f) -> str:
    return f"{f / 1e6:.5f}"


def _table(headings, stretch):
    t = sortable(QTableWidget(0, len(headings)))
    t.setHorizontalHeaderLabels(headings)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    hh = t.horizontalHeader()
    for i in range(len(headings)):
        hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
    hh.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    return t


def _fill(table, rows):
    """rows: (key, [cells], tooltip)."""
    keep = None
    cur = table.currentRow()
    if cur >= 0 and table.item(cur, 0) is not None:
        keep = table.item(cur, 0).data(Qt.ItemDataRole.UserRole)
    with filling(table):
        table.setRowCount(len(rows))
        for r, (key, cells, tip) in enumerate(rows):
            for c, text in enumerate(cells):
                item = SortItem(str(text))
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, key)
                if tip:
                    item.setToolTip(tip)
                table.setItem(r, c, item)
    if keep is not None:
        for r in range(table.rowCount()):
            if table.item(r, 0).data(Qt.ItemDataRole.UserRole) == keep:
                table.blockSignals(True)
                table.selectRow(r)
                table.blockSignals(False)
                break


def _count(n, word):
    return f"{n} {word}{'s' if n != 1 else ''}"


class ExplorerPane(QWidget):
    only = pyqtSignal(object)            # None (everything), ("freq", Hz), ("tg", n), ("radio", n)
    tune = pyqtSignal(float)             # double-click a channel: listen to it in the receiver

    def __init__(self):
        super().__init__()
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.title = FitLabel("Exploring: signals in view are found and tried by themselves")
        self.all_btn = QPushButton("Listen to everything")
        self.only_btn = QPushButton("Only the selected")
        self.only_btn.setToolTip("Hear only the selected channel, talkgroup or radio; the rest are muted while "
                                 "their calls are on")
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.title, 1)
        head.addWidget(self.all_btn)
        head.addWidget(self.only_btn)
        self.channels = _table(["MHz", "What it is", "Access code", "Slots", "Calls", "On now", "Decoded",
                                "Last heard"], 6)
        self.channels.setToolTip("Every signal found. Hover a row for what it means; double-click to tune there.")
        self.systems = _table(["Protocol", "System", "Kind", "Control channels", "Voice channels", "Calls",
                               "Last heard"], 4)
        self.talkgroups = _table(["Talkgroup", "Kind", "Calls", "Radios", "Channels", "Encrypted", "Last heard"], 4)
        self.radios = _table(["Radio ID", "Last talkgroup", "Calls", "Talkgroups", "Channels", "Position",
                              "Last heard"], 5)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setFont(mono)
        self.activity.setMaximumBlockCount(5000)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.channels, "Channels")
        self.tabs.addTab(self.systems, "Systems")
        self.tabs.addTab(self.talkgroups, "Talkgroups")
        self.tabs.addTab(self.radios, "Radios")
        self.tabs.addTab(self.activity, "Activity")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addLayout(head)
        lay.addWidget(self.tabs, 1)
        self.all_btn.clicked.connect(lambda: self.only.emit(None))
        self.only_btn.clicked.connect(self._only_selected)
        self.channels.doubleClicked.connect(lambda _: self._tune())

    def _selected(self, table):
        r = table.currentRow()
        return table.item(r, 0).data(Qt.ItemDataRole.UserRole) if r >= 0 and table.item(r, 0) else None

    def _only_selected(self):
        tab = self.tabs.currentWidget()
        kind = {self.channels: "freq", self.talkgroups: "tg", self.radios: "radio"}.get(tab)
        key = self._selected(tab) if kind else None
        if key is not None:
            self.only.emit((kind, key))

    def _tune(self):
        f = self._selected(self.channels)
        if f is not None:
            self.tune.emit(float(f))

    def add_log(self, t, freq, text):
        self.activity.appendPlainText(f"{time.strftime('%H:%M:%S', time.localtime(t))}  {mhz(freq)}  {text}")

    def refresh(self, ex, decoding=(), listening=""):
        now = time.time()
        digital = [f for f, s in ex.states.items() if s == "digital"]
        other = sum(1 for s in ex.states.values() if s == "not digital")
        self.title.setText(f"{len(digital)} digital voice channel{'s' if len(digital) != 1 else ''} found, "
                           f"{len(decoding)} being decoded now, {other} other signal{'s' if other != 1 else ''} · "
                           f"{_count(len(ex.talkgroups), 'talkgroup')}, {_count(len(ex.radios), 'radio')} · "
                           f"listening to {listening}")
        rows = []
        for f in sorted(set(ex.channels) | set(ex.states)):
            ch = ex.channels.get(f)
            state = ex.states.get(f, "")
            if ch is None or not ch.proto:
                what = {"probing": "trying it…", "not digital": "not digital voice",
                        "idle": "quiet"}.get(state, state)
                rows.append((f, [mhz(f), what, "", "", "", "", "", ""], ""))
                continue
            on = ""
            if ch.calling and ch.last_call:
                c = ch.last_call
                on = f"{'group' if c['group'] else 'private'} {c['tgt']} ← {c['src']}"
            rows.append((f, [mhz(f), ch.what() + ("" if f in decoding or state == "digital" else " (not watched now)"),
                             ch.code(), " ".join(str(s) for s in sorted(ch.slots)), ch.calls, on, ch.contents(),
                             ago(ch.last, now)], ch.explain()))
        _fill(self.channels, rows)
        _fill(self.systems, [((s["proto"], s["id"]), [s["proto"], s["id"], s["system"] or
                              ("trunked" if s["control"] else "conventional"),
                              " ".join(mhz(f) for f in sorted(s["control"])),
                              " ".join(mhz(f) for f in sorted(s["voice"])), s["calls"], ago(s["last"], now)], "")
                             for s in ex.systems()])
        _fill(self.talkgroups, [(k, [k, "group" if v.get("group", True) else "private", v["calls"], len(v["radios"]),
                                     " ".join(mhz(f) for f in sorted(v["freqs"])),
                                     f"{v['encrypted']} of {v['calls']}" if v.get("encrypted") else "",
                                     ago(v.get("last"), now)], "")
                                for k, v in ex.talkgroups.items()])
        _fill(self.radios, [(k, [k, v.get("last_tg", ""), v["calls"], " ".join(str(t) for t in sorted(v["talkgroups"])),
                                 " ".join(mhz(f) for f in sorted(v["freqs"])),
                                 f"{v['position'][0]:.5f}, {v['position'][1]:.5f}" if v.get("position") else "",
                                 ago(v.get("last"), now)], "")
                            for k, v in ex.radios.items()])
