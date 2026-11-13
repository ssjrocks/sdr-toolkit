"""The Details tab, the Logbook window and the Replay window."""

import bisect
import csv
import html
import json
import time
from datetime import datetime

from PyQt6.QtCore import QDateTime, QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QSizePolicy,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import photos
from .mapview import MapView
from .widgets import SortItem, filling, sortable, user_columns

CRAFT_MODES = {"adsb": "ADS-B aircraft", "ais": "AIS ships"}


def when(t) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


# ---- photos ---------------------------------------------------------------

class Photos(QObject):
    """Finds and downloads the Details photo (see photos.py). Answers are kept for the session."""
    ready = pyqtSignal(str, object)          # request key, {"pixmap", "credit", "link", "own"} or None

    def __init__(self, parent=None, online=True):
        super().__init__(parent)
        self.online = online
        self._net = QNetworkAccessManager(self)
        self._cache: dict[str, dict | None] = {}
        self._busy: set[str] = set()

    def request(self, photo: dict) -> str:
        key = json.dumps(photo, sort_keys=True)
        if key in self._cache:
            QTimer.singleShot(0, lambda: self.ready.emit(key, self._cache[key]))
        elif self.online and key not in self._busy:
            self._busy.add(key)
            first = photos.first_request(photo)
            if first is None:
                self._done(key, None)
            else:
                self._step(key, photo, *first)
        return key

    def _get(self, url, callback):
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", photos.USER_AGENT.encode())
        reply = self._net.get(req)
        reply.finished.connect(lambda: self._got(reply, callback))

    def _got(self, reply, callback):
        try:
            ok = reply.error() == QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll()) if ok else b""
        finally:
            reply.deleteLater()
        callback(data)

    def _step(self, key, photo, step, url):
        def answer(data):
            try:
                doc = json.loads(data) if data else {}
            except ValueError:
                doc = {}
            if step == "planespotters":
                found = photos.planespotters(doc)
                if found:
                    return self._image(key, found)
                nxt = photos.type_request(photo)
                return self._step(key, photo, *nxt) if nxt else self._done(key, None)
            if step == "search":
                title = photos.search_title(doc)
                if not title:
                    return self._done(key, None)
                return self._step(key, photo, "summary", photos.WIKI_SUMMARY.format(title=title))
            found = photos.summary(doc)
            return self._image(key, found) if found else self._done(key, None)
        self._get(url, answer)

    def _image(self, key, found):
        def answer(data):
            pix = QPixmap()
            if data and pix.loadFromData(data):
                self._done(key, dict(found, pixmap=pix))
            else:
                self._done(key, None)
        self._get(found["image"], answer)

    def _done(self, key, result):
        self._busy.discard(key)
        self._cache[key] = result
        self.ready.emit(key, result)


# ---- Details tab ------------------------------------------------------------

class DetailsPanel(QScrollArea):
    """What's known about the aircraft or ship picked in the table or on the map."""

    def __init__(self, photo_source: Photos):
        super().__init__()
        self.setWidgetResizable(True)
        self.photos = photo_source
        self.photos.ready.connect(self._photo_ready)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        self.title = QLabel()
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.4)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setWordWrap(True)
        self.subtitle = QLabel()
        self.subtitle.setWordWrap(True)
        self.photo = QLabel()
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo.setMinimumHeight(60)
        self.photo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._pixmap = None
        self.credit = QLabel()
        self.credit.setOpenExternalLinks(True)
        self.credit.setWordWrap(True)
        self.fields = QLabel()
        self.fields.setTextFormat(Qt.TextFormat.RichText)
        self.fields.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.fields.setWordWrap(True)
        self.links = QLabel()
        self.links.setOpenExternalLinks(True)
        self.links.setWordWrap(True)
        for w in (self.title, self.subtitle, self.photo, self.credit, self.fields, self.links):
            lay.addWidget(w)
        lay.addStretch(1)
        self.setWidget(inner)
        self.key = None
        self._photo_key = None
        self.clear()

    def clear(self, text="Click an aircraft or ship in the table or on the map to see its details here."):
        self.key = None
        self._photo_key = None
        self.title.setText("")
        self.subtitle.setText(text)
        self.photo.clear()
        self._pixmap = None
        self.photo.setVisible(False)
        self.credit.setText("")
        self.fields.setText("")
        self.links.setText("")

    def show_details(self, key, details: dict | None, logged: dict | None = None, heard=True):
        """*details* from the mode (None once it's no longer heard); *logged* from the logbook."""
        new = key != self.key
        self.key = key
        if details is None and logged is None:
            return self.clear(f"{key}: nothing known.")
        if details is None:
            info = logged.get("info", {})
            details = {"title": logged.get("name") or key,
                       "subtitle": "Not heard now",
                       "fields": [(k, v) for k, v in info.items() if v not in ("", None)],
                       "photo": None, "links": []}
        self.title.setText(html.escape(details.get("title", key)))
        sub = details.get("subtitle", "")
        if not heard:
            sub = (sub + " · " if sub else "") + "not heard now"
        self.subtitle.setText(html.escape(sub))
        rows = list(details.get("fields", []))
        if logged:
            rows.append(("First logged", when(logged["first"])))
            rows.append(("Times seen", str(logged["sightings"])))
        self.fields.setText("<table cellspacing=3>" + "".join(
            f"<tr><td style='color:gray'>{html.escape(str(a))}</td><td>{html.escape(str(b))}</td></tr>"
            for a, b in rows) + "</table>")
        self.links.setText(" · ".join(f"<a href='{html.escape(u)}'>{html.escape(t)}</a>"
                                      for t, u in details.get("links", [])))
        photo = details.get("photo")
        if new and photo:
            self.photo.setVisible(True)
            self._pixmap = None
            self.photo.setText("Looking for a photo…")
            self.credit.setText("")
            self._photo_key = self.photos.request(photo)
        elif new:
            self.photo.setVisible(False)

    def _fit_photo(self):
        if self._pixmap is not None:
            width = max(120, min(self.viewport().width() - 24, self._pixmap.width()))
            self.photo.setPixmap(self._pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_photo()

    def _photo_ready(self, key, result):
        if key != self._photo_key:
            return
        if not result:
            self.photo.setText("No photo found.")
            self.credit.setText("")
            return
        self._pixmap = result["pixmap"]
        self._fit_photo()
        self.credit.setText(f"<a href='{html.escape(result['link'])}'>{html.escape(result['credit'])}</a>")


# ---- Logbook window ---------------------------------------------------------

class LogbookWindow(QDialog):
    def __init__(self, win, logbook, mode_names: dict):
        super().__init__(win)
        self.win = win
        self.logbook = logbook
        self.mode_names = mode_names
        self.setWindowTitle("Logbook")
        self.resize(900, 600)
        top = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem("All modes", "")
        for mid, name in mode_names.items():
            self.mode.addItem(name, mid)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search (callsign, registration, name, text…)")
        self.search.returnPressed.connect(self.refresh)
        self.mode.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.mode)
        top.addWidget(self.search, 1)
        self.tabs = QTabWidget()
        self.received = self._table(("Time", "Mode", "Received"))
        self.spotted = self._table(("Mode", "ID", "Name", "Details", "First seen", "Last seen", "Times seen"))
        self.tabs.addTab(self.received, "Received")
        self.tabs.addTab(self.spotted, "Aircraft and ships")
        self.note = QLabel()
        btns = QHBoxLayout()
        for text, fn in (("Refresh", self.refresh), ("Replay…", self.open_replay), ("Export CSV…", self.export),
                         ("Clear…", self.clear)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.note)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(btns)

    def _table(self, headings):
        t = sortable(QTableWidget(0, len(headings)))
        t.setHorizontalHeaderLabels(headings)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.verticalHeader().setVisible(False)
        user_columns(t)
        return t

    def show_mode(self, mode_id):
        i = self.mode.findData(mode_id)
        self.mode.setCurrentIndex(max(i, 0))
        self.tabs.setCurrentIndex(1 if mode_id in CRAFT_MODES else 0)
        self.refresh()

    def refresh(self):
        self.win.logbook.flush()
        mode, search = self.mode.currentData(), self.search.text().strip()
        rows = self.logbook.entries(mode or None, search)
        self._fill(self.received, [(when(t), self.mode_names.get(m, m), text) for t, m, text in rows])
        craft = self.logbook.craft(mode or None, search)
        self._fill(self.spotted, [(self.mode_names.get(c["mode"], c["mode"]), c["key"].upper(), c["name"],
                                   ", ".join(f"{v}" for k, v in c["info"].items()
                                             if k in ("r", "t", "desc", "type", "callsign", "destination", "ownOp")),
                                   when(c["first"]), when(c["last"]), c["sightings"]) for c in craft])
        n = self.logbook.counts(mode or None)
        self.note.setText(f"{n['entries']} received, {n['craft']} aircraft/ships, {n['positions']} positions")

    def _fill(self, table, rows):
        with filling(table):
            table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    table.setItem(r, c, SortItem(str(value)))

    def export(self):
        table = self.tabs.currentWidget()
        path, _ = QFileDialog.getSaveFileName(self, "Export", "logbook.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([table.horizontalHeaderItem(c).text() for c in range(table.columnCount())])
            for r in range(table.rowCount()):
                w.writerow([table.item(r, c).text() if table.item(r, c) else "" for c in range(table.columnCount())])

    def clear(self):
        mode = self.mode.currentData()
        what = self.mode_names.get(mode, "every mode") if mode else "every mode"
        if QMessageBox.question(self, "Clear the logbook",
                                f"Delete everything logged for {what}? This can't be undone.") \
                == QMessageBox.StandardButton.Yes:
            self.logbook.clear(mode or None)
            self.refresh()

    def open_replay(self):
        mode = self.mode.currentData()
        ReplayWindow(self.win, self.logbook, mode if mode in CRAFT_MODES else "adsb").show()


# ---- Replay -----------------------------------------------------------------

class ReplayWindow(QDialog):
    """Plays logged ADS-B / AIS positions back on a map."""
    KEEP = {"adsb": 60, "ais": 30 * 60}          # a craft disappears when not heard for this long (s)
    TRAIL = {"adsb": 10 * 60, "ais": 2 * 3600}

    def __init__(self, win, logbook, mode="adsb", online=True):
        super().__init__(win)
        self.logbook = logbook
        self.setWindowTitle("Replay")
        self.resize(1000, 720)
        self.mode = QComboBox()
        for mid, name in CRAFT_MODES.items():
            self.mode.addItem(name, mid)
        self.mode.setCurrentIndex(max(self.mode.findData(mode), 0))
        self.start = QDateTimeEdit()
        self.end = QDateTimeEdit()
        for w in (self.start, self.end):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd HH:mm")
        load = QPushButton("Load")
        load.clicked.connect(self.load)
        self.mode.currentIndexChanged.connect(self._default_range)
        top = QHBoxLayout()
        for w in (QLabel("Show"), self.mode, QLabel("from"), self.start, QLabel("to"), self.end, load):
            top.addWidget(w)
        top.addStretch(1)
        self.map = MapView(online=online)
        self.play = QPushButton("Play")
        self.play.setCheckable(True)
        self.play.toggled.connect(lambda on: self.play.setText("Pause" if on else "Play"))
        self.speed = QComboBox()
        for x in (1, 10, 30, 60, 300, 1000):
            self.speed.addItem(f"{x}×", x)
        self.speed.setCurrentIndex(3)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderMoved.connect(self._seek)
        self.clock = QLabel()
        bottom = QHBoxLayout()
        for w in (self.play, self.speed):
            bottom.addWidget(w)
        bottom.addWidget(self.slider, 1)
        bottom.addWidget(self.clock)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.map, 1)
        lay.addLayout(bottom)
        self.tracks: dict[str, tuple[list, list]] = {}
        self.t0 = self.t1 = self.now = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)
        self._default_range()

    def _default_range(self):
        rng = self.logbook.time_range(self.mode.currentData())
        end = rng[1] if rng else time.time()
        start = max(rng[0], end - 3600) if rng else end - 3600
        self.start.setDateTime(QDateTime.fromSecsSinceEpoch(int(start)))
        self.end.setDateTime(QDateTime.fromSecsSinceEpoch(int(end) + 60))
        self.load()

    def load(self):
        mode = self.mode.currentData()
        self.t0 = float(self.start.dateTime().toSecsSinceEpoch())
        self.t1 = float(self.end.dateTime().toSecsSinceEpoch())
        self.tracks = {}
        for key, t, lat, lon, alt, heading, label in self.logbook.positions(mode, self.t0, self.t1):
            times, rows = self.tracks.setdefault(key, ([], []))
            times.append(t)
            rows.append((lat, lon, heading, label))
        self.now = self.t0
        self.map.clear_session()
        self.map.follow = True
        self._show()
        self.clock.setText(self.clock.text() + (f" · {len(self.tracks)} in this range" if self.tracks else
                                                " · nothing logged in this range"))

    def points_at(self, t) -> list[dict]:
        mode = self.mode.currentData()
        keep, trail = self.KEEP[mode], self.TRAIL[mode]
        points = []
        for key, (times, rows) in self.tracks.items():
            i = bisect.bisect_right(times, t) - 1
            if i < 0 or t - times[i] > keep:
                continue
            j = bisect.bisect_left(times, t - trail)
            lat, lon, heading, label = rows[i]
            points.append({"id": key, "lat": lat, "lon": lon, "heading": heading, "label": label or key.upper(),
                           "kind": "aircraft" if mode == "adsb" else "ship",
                           "track": [(r[0], r[1]) for r in rows[j:i + 1]]})
        return points

    def _show(self):
        self.map.set_points(self.points_at(self.now), None)
        span = max(self.t1 - self.t0, 1)
        self.slider.blockSignals(True)
        self.slider.setValue(int((self.now - self.t0) / span * 1000))
        self.slider.blockSignals(False)
        self.clock.setText(when(self.now))

    def _seek(self, value):
        self.now = self.t0 + (self.t1 - self.t0) * value / 1000
        self._show()

    def _tick(self):
        if not self.play.isChecked():
            return
        self.now = min(self.now + 0.2 * self.speed.currentData(), self.t1)
        if self.now >= self.t1:
            self.play.setChecked(False)
        self._show()


# ---- Connect relay ------------------------------------------------------------

class RelayDialog(QDialog):
    """Find an SDR Tools Relay (this network, ssjrocks.com's list, or an address) and connect to it."""

    def __init__(self, win, settings):
        super().__init__(win)
        from . import relay
        self.relay = relay
        self.win = win
        self.settings = settings
        self.bridge = None
        self.setWindowTitle("Connect relay")
        self.resize(640, 460)
        intro = QLabel("Use an RTL-SDR plugged into another PC running "
                       "<a href='https://github.com/ssjrocks/sdr-tools-relay'>SDR Tools Relay</a>. While connected, "
                       "the RTL-SDR modes use its dongle instead of one on this PC.")
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        self.tabs = QTabWidget()
        self.lan = QTableWidget(0, 4)
        self.net = QTableWidget(0, 5)
        for t, heads in ((self.lan, ("Name", "Address", "Dongle", "")),
                         (self.net, ("Name", "Location", "Address", "Dongle", ""))):
            sortable(t)
            t.setHorizontalHeaderLabels(heads)
            t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            t.verticalHeader().setVisible(False)
            user_columns(t)
            t.doubleClicked.connect(self.connect_selected)
        scan = QPushButton("Scan this network")
        scan.clicked.connect(self.scan)
        refresh = QPushButton("Get the list from ssjrocks.com")
        refresh.clicked.connect(self.fetch)
        for table, button, title in ((self.lan, scan, "On this network"), (self.net, refresh, "Internet")):
            page = QWidget()
            pl = QVBoxLayout(page)
            pl.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
            pl.addWidget(table, 1)
            self.tabs.addTab(page, title)
        addr_page = QWidget()
        al = QVBoxLayout(addr_page)
        self.address = QLineEdit(str(settings.value("relay/address", "")))
        self.address.setPlaceholderText(f"host or IP, optionally :port (default {relay.PORT}), e.g. a Tailscale name")
        al.addWidget(QLabel("Address"))
        al.addWidget(self.address)
        al.addStretch(1)
        self.tabs.addTab(addr_page, "By address")
        self.tabs.setCurrentIndex(int(settings.value("relay/tab", 0)))
        self.password = QLineEdit(str(settings.value("relay/password", "")))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("if the relay has one")
        self.note = QLabel()
        self.note.setWordWrap(True)
        connect = QPushButton("Connect")
        connect.setDefault(True)
        connect.clicked.connect(self.connect_selected)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Password"))
        bottom.addWidget(self.password, 1)
        bottom.addWidget(connect)
        bottom.addWidget(cancel)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.note)
        lay.addLayout(bottom)
        self._found = {"lan": [], "net": []}
        QTimer.singleShot(0, self.scan if self.tabs.currentIndex() == 0 else
                          self.fetch if self.tabs.currentIndex() == 1 else lambda: None)

    def _fill(self, table, rows, data):
        with filling(table):
            table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, text in enumerate(row):
                    item = SortItem(str(text))
                    if c == 0:
                        item.setData(Qt.ItemDataRole.UserRole, data[r])
                    table.setItem(r, c, item)
        if rows:
            table.selectRow(0)

    @staticmethod
    def _flags(r) -> str:
        return " · ".join(x for x in ("password" if r.get("password") else "", "in use" if r.get("busy") else "") if x)

    def scan(self):
        self.note.setText("Looking for relays on this network…")
        self.win._run_task(lambda: self.relay.discover(), self._scanned,
                           lambda msg: self.note.setText(f"Scan failed: {msg}"))

    def _scanned(self, found):
        devs = lambda r: ", ".join(d.get("name", "") for d in r.get("devices", [])) or "no dongle"  # noqa: E731
        self._fill(self.lan, [(r.get("name", ""), f"{r['host']}:{r.get('port')}", devs(r), self._flags(r))
                              for r in found], found)
        if self.tabs.currentIndex() != 0:
            return                          # don't cover a message about the relay being connected to
        self.note.setText(f"{len(found)} relay{'s' if len(found) != 1 else ''} on this network." if found else
                          "No relays answered. Is SDR Tools Relay running on the other PC, and is UDP port "
                          f"{self.relay.DISCOVERY_PORT} allowed through its firewall?")

    def fetch(self):
        self.note.setText("Getting the list from ssjrocks.com…")
        self.win._run_task(lambda: self.relay.directory(), self._fetched,
                           lambda msg: self.note.setText(f"Couldn't get the list: {msg}"))

    def _fetched(self, found):
        self._fill(self.net, [(r.get("name", ""), r.get("location", ""), f"{r['host']}:{r['port']}",
                               ", ".join(r.get("devices") or []) or "no dongle", self._flags(r)) for r in found], found)
        if self.tabs.currentIndex() != 1:
            return
        self.note.setText(f"{len(found)} relay{'s' if len(found) != 1 else ''} listed." if found else
                          "No relays are listed at the moment.")

    def target(self) -> dict | None:
        i = self.tabs.currentIndex()
        if i == 2:
            text = self.address.text().strip()
            if not text:
                return None
            host, _, port = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
            return {"host": host or text, "port": int(port) if port.isdigit() else self.relay.PORT, "name": ""}
        table = self.lan if i == 0 else self.net
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        return table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    def connect_selected(self):
        t = self.target()
        if t is None:
            self.note.setText("Pick a relay first.")
            return
        password = self.password.text()
        self.note.setText(f"Connecting to {t.get('name') or t['host']}…")

        def done(bridge):
            s = self.settings
            s.setValue("relay/tab", self.tabs.currentIndex())
            s.setValue("relay/address", self.address.text().strip())
            s.setValue("relay/password", password)
            self.bridge = bridge
            self.accept()

        self.win._run_task(lambda: self.relay.connect(t["host"], t["port"], password, t.get("name", "")), done,
                           lambda msg: self.note.setText(msg))
