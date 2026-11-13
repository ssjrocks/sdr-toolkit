"""The DMR / P25 explorer's Calls tab (in the right sidebar): every single transmission it saved, to
browse by network, talkgroup or radio and play again. The selected call's details are at the bottom,
with Play and Show in folder."""

import html
import time
from pathlib import Path

from PyQt6.QtCore import QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter,
                             QTableWidget, QVBoxLayout, QWidget)

from .widgets import SortItem, filling, following, hovered, reselect, selected_key, sortable, user_columns

COLUMNS = ("Time", "Talkgroup", "Radio", "Length", "Network", "MHz", "Kind")   # the narrow sidebar shows the first
KEEP = 20000                    # calls listed at most (the files stay)


def when(t) -> str:
    lt = time.localtime(t)
    return time.strftime("%H:%M:%S" if time.localtime()[:3] == lt[:3] else "%Y-%m-%d %H:%M:%S", lt)


class CallsPanel(QWidget):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.calls: list = []                       # receiver/calls.Call, oldest first
        self._by_path: dict = {}
        self._pending = False                       # new calls not shown yet (the mouse was over the table)
        self.network = QComboBox()
        self.network.addItem("All networks", "")
        self.network.setToolTip("Calls of one network: a DMR colour code, a P25 system or NAC. Several are often "
                                "heard at once.")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Talkgroup, radio or MHz…")
        self.search.setClearButtonEnabled(True)
        self.table = sortable(QTableWidget(0, len(COLUMNS)))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        user_columns(self.table)
        self.table.setToolTip("Every call saved. Click one for its details; double-click to play it.")
        self.count = QLabel("")
        self.details = QLabel("Pick a call for its details.")
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.play_btn = QPushButton("Play")
        self.play_btn.setEnabled(False)
        self.folder_btn = QPushButton("Show in folder")
        self.folder_btn.setEnabled(False)
        self.copy_btn = QPushButton("Copy details")
        self.copy_btn.setEnabled(False)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.network, 1)
        top.addWidget(self.search, 1)
        upper = QWidget()
        ul = QVBoxLayout(upper)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.addLayout(top)
        ul.addWidget(self.table, 1)
        ul.addWidget(self.count)
        lower = QWidget()
        ll = QVBoxLayout(lower)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.details, 1)
        btns = QHBoxLayout()
        btns.addWidget(self.play_btn)
        btns.addWidget(self.folder_btn)
        btns.addWidget(self.copy_btn)
        btns.addStretch(1)
        ll.addLayout(btns)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(upper)
        split.addWidget(lower)
        split.setSizes([500, 200])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)
        self.network.currentIndexChanged.connect(lambda _: self._show(force=True))
        self.search.textChanged.connect(lambda _: self._show(force=True))
        self.table.itemSelectionChanged.connect(self._picked)
        self.table.doubleClicked.connect(lambda _: self.play())
        self.play_btn.clicked.connect(self.play)
        self.folder_btn.clicked.connect(self.show_folder)
        self.copy_btn.clicked.connect(self._copy)
        self._player = None
        self._playing = None

    # ---- calls in ----------------------------------------------------------------------------

    def set_networks(self, names):
        have = {self.network.itemData(i) for i in range(self.network.count())}
        for n in names:
            if n and n not in have:
                self.network.addItem(n, n)

    def add(self, calls):
        """New calls (receiver/calls.Call). Shown at once unless the mouse is over the table: then
        when it leaves (the next add or tick)."""
        for c in calls:
            if c.path not in self._by_path:
                self._by_path[c.path] = c
                self.calls.append(c)
                if c.network:
                    self.set_networks([c.network])
        del self.calls[:-KEEP]
        if calls or self._pending:
            self._show()

    def tick(self):
        if self._pending:
            self._show()

    def _match(self, c) -> bool:
        net, q = self.network.currentData(), self.search.text().strip().lower()
        if net and c.network != net:
            return False
        if q:
            text = f"{c.talkgroup} {c.radio} {c.freq / 1e6:.5f} {c.network} {c.kind}".lower()
            return all(w in text for w in q.split())
        return True

    def _show(self, force=False):
        t = self.table
        if hovered(t) and not force:
            self._pending = True
            return
        self._pending = False
        shown = [c for c in self.calls if self._match(c)]
        keep = selected_key(t)
        follow = following(t)
        blocked = t.blockSignals(True)
        try:
            with filling(t):
                t.setRowCount(len(shown))
                for r, c in enumerate(shown):
                    cells = (when(c.start), "" if c.talkgroup is None else str(c.talkgroup),
                             "" if c.radio is None else str(c.radio), f"{c.length:.1f} s", c.network or "?",
                             f"{c.freq / 1e6:.5f}", c.kind)
                    for col, text in enumerate(cells):
                        item = t.item(r, col)
                        if item is None:
                            item = SortItem(text)
                            t.setItem(r, col, item)
                        else:
                            item.setText(text)
                        if col == 0:
                            item.setData(Qt.ItemDataRole.UserRole, c.path)
        finally:
            t.blockSignals(blocked)
        if keep is not None:
            for r in range(t.rowCount()):
                if t.item(r, 0).data(Qt.ItemDataRole.UserRole) == keep:
                    reselect(t, r)
                    break
        if follow:
            t.scrollToBottom()
        n = len(self.calls)
        self.count.setText(f"{len(shown)} of {n} call{'s' if n != 1 else ''}" if len(shown) != n else
                           f"{n} call{'s' if n != 1 else ''}")

    # ---- the picked call ------------------------------------------------------------------------

    def selected(self):
        path = selected_key(self.table)
        return self._by_path.get(path) if path else None

    def _picked(self):
        c = self.selected()
        for b in (self.play_btn, self.folder_btn, self.copy_btn):
            b.setEnabled(c is not None)
        if c is None:
            self.details.setText("Pick a call for its details.")
            return
        rows = [("When", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c.start))),
                ("Length", f"{c.length:.1f} s"), ("Frequency", f"{c.freq / 1e6:.5f} MHz"),
                ("Network", c.network or "not known yet when it was saved"),
                ("Talkgroup", "" if c.talkgroup is None else str(c.talkgroup)),
                ("Radio", "" if c.radio is None else str(c.radio)), ("Kind", f"{c.kind} call" if c.kind else ""),
                ("File", c.path)]
        self._plain = "\n".join(f"{k}: {v}" for k, v in rows if v)
        self.details.setText("<table>" + "".join(
            f"<tr><td><b>{html.escape(k)}</b>&nbsp;&nbsp;</td><td>{html.escape(v)}</td></tr>" for k, v in rows if v)
            + "</table>")
        self.play_btn.setText("Stop" if self._playing == c.path else "Play")

    def play(self):
        c = self.selected()
        if c is None:
            return
        if self._player is None:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._player.playbackStateChanged.connect(self._state)
        if self._playing == c.path and self._player.playbackState() == self._player.PlaybackState.PlayingState:
            self._player.stop()
            return
        self._playing = c.path
        self._player.setSource(QUrl.fromLocalFile(c.path))
        self._player.play()
        self.play_btn.setText("Stop")

    def _state(self, state):
        if state == self._player.PlaybackState.StoppedState:
            self._playing = None
            self.play_btn.setText("Play")

    def show_folder(self):
        c = self.selected()
        if c is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(c.path).parent)))

    def _copy(self):
        if self.selected() is not None:
            QGuiApplication.clipboard().setText(self._plain)
