"""The Satellite tracker page: the list, the map, passes, radios and the sky plot."""

import html
import math
import time

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..widgets import FitLabel, SortItem, filling, jump_to, reselect, sortable, unsorted, user_columns
from .data import CATEGORIES, FEATURES, pick, short

COMING_HOURS = 12                   # the Coming over tab looks this far ahead
MAX_ON_MAP = 300                    # satellites shown on the map with none picked
REFRESH = 1.5                       # s between refreshes while the next passes are coming in
KIND_NAMES = {"listen": "Listen", "decode": "Decode", "pictures": "Pictures", "telemetry": "Telemetry",
              "out": "Out of range"}


def when(t, now=None) -> str:
    """A local time, with the day when it isn't today."""
    lt = time.localtime(t)
    today = time.localtime(now or time.time())
    if lt.tm_yday == today.tm_yday and lt.tm_year == today.tm_year:
        return time.strftime("%H:%M:%S", lt)
    return time.strftime("%a %d %b %H:%M", lt)


def span(seconds) -> str:
    seconds = int(max(seconds, 0))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    return f"{seconds // 60}:{seconds % 60:02d}"


def compass(az) -> str:
    return ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW",
            "NNW")[int((az % 360) / 22.5 + 0.5) % 16]


def _table(headings, stretch=-1):
    t = sortable(QTableWidget(0, len(headings)))
    t.setHorizontalHeaderLabels(headings)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    user_columns(t)
    if stretch % len(headings) != len(headings) - 1:   # a wide column in the middle: start it wider
        t.horizontalHeader().resizeSection(stretch % len(headings), 260)
        t._by_hand.add(stretch % len(headings))
    return t


def _fill(table, rows, keys=None):
    """rows: lists of cell texts; keys: something to keep on each row's first cell. The table's
    selection signals are quiet meanwhile: rows changing under the selection isn't the user picking."""
    blocked = table.blockSignals(True)
    try:
        _write(table, rows, keys)
    finally:
        table.blockSignals(blocked)


def _write(table, rows, keys):
    with filling(table):
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                item = SortItem(str(text))
                if c == 0 and keys is not None:
                    item.setData(Qt.ItemDataRole.UserRole, keys[r])
                table.setItem(r, c, item)


def _key(table, row):
    item = table.item(row, 0) if row >= 0 else None
    return item.data(Qt.ItemDataRole.UserRole) if item is not None else None


class SkyPlot(QWidget):
    """A pass seen from where you are: the horizon round the edge, straight up in the middle."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(220, 220)
        self.path = None                # (az, el) arrays of the pass
        self.now = None                 # (az, el) where it is, if it's up
        self.title = ""

    def set_pass(self, az, el, title=""):
        self.path = (np.asarray(az), np.asarray(el)) if az is not None else None
        self.title = title
        self.update()

    def _xy(self, az, el, c, r):
        d = r * (90 - np.clip(el, 0, 90)) / 90
        a = np.radians(az)
        return c.x() + d * np.sin(a), c.y() - d * np.cos(a)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(12, 16, 28))
        r = min(self.width(), self.height() - 24) / 2 - 18
        c = QPointF(self.width() / 2, self.height() / 2 + 10)
        p.setPen(QPen(QColor(70, 85, 110), 1))
        for el in (0, 30, 60):
            d = r * (90 - el) / 90
            p.drawEllipse(c, d, d)
        p.drawLine(QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - r), QPointF(c.x(), c.y() + r))
        p.setPen(QColor(170, 180, 200))
        for label, az in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            x, y = self._xy(az, -8, c, r)
            p.drawText(QRectF(x - 10, y - 9, 20, 18), Qt.AlignmentFlag.AlignCenter, label)
        for el in (30, 60):
            p.drawText(QPointF(c.x() + 3, c.y() - r * (90 - el) / 90 - 2), f"{el}°")
        if self.title:
            p.drawText(QRectF(4, 2, self.width() - 8, 18), Qt.AlignmentFlag.AlignLeft, self.title)
        if self.path is not None and len(self.path[0]):
            az, el = self.path
            xs, ys = self._xy(az, el, c, r)
            p.setPen(QPen(QColor(90, 200, 255), 2))
            p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in zip(xs, ys)]))
            p.setBrush(QColor(90, 220, 120))
            p.drawEllipse(QPointF(xs[0], ys[0]), 4, 4)            # rises here
            p.setBrush(QColor(230, 90, 80))
            p.drawEllipse(QPointF(xs[-1], ys[-1]), 4, 4)          # sets here
        if self.now is not None:
            x, y = self._xy(self.now[0], self.now[1], c, r)
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.setBrush(QColor(255, 200, 40))
            p.drawEllipse(QPointF(x, y), 6, 6)
        p.end()


class SatellitePanel(QWidget):
    listen = pyqtSignal(object, object)     # (Satellite, Radio): tune Spectrum & receiver there
    status = pyqtSignal(str)

    def __init__(self, map_view):
        super().__init__()
        self.catalogue: dict = {}
        self.next_passes: dict = {}
        self._refreshed = 0.0
        self.home = None
        self.min_el, self.days, self.only_hear = 10.0, 5, True
        self.selected = None              # Satellite
        self._sat = None                  # its satrec
        self._passes: list = []
        self._ground: dict = {}
        # The list
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search: name, NORAD number…")
        self.category = QComboBox()
        self.category.addItem("Everything", "")
        for c in CATEGORIES:
            self.category.addItem(c, c)
        self.category.insertSeparator(self.category.count())
        for f in FEATURES:                              # what they carry: all with a ham repeater, all SSTV...
            self.category.addItem(f"With {f[0].lower() + f[1:]}", f"has:{f}")
        self.category.setCurrentIndex(self.category.findData("Fun to listen to"))
        self.category.setToolTip("Which satellites to list: a kind, or the ones carrying something (heard lately)")
        self.list = _table(["Satellite", "Next pass", "Highest", "Kind"], stretch=3)
        self.list.setToolTip("Click a satellite for its passes and radios. Click a heading to sort.")
        # The map, and what it's doing now
        self.map = map_view
        self.map.keep_session = False       # where satellites were isn't worth keeping: their passes are
        self.map.tracks_btn.hide()
        self.now_label = FitLabel("Pick a satellite")
        self.now_label.setFont(QFont(self.now_label.font().family(), -1, QFont.Weight.Bold))
        # Tabs
        self.coming = _table(["Rises", "Satellite", "Highest", "Lasts", "Visible", "What's on it"], stretch=5)
        self.coming.setToolTip(f"Every listed satellite's passes in the next {COMING_HOURS} hours, soonest first")
        self.pass_table = _table(["Rises", "Highest at", "Highest", "Sets", "Lasts", "Rises in", "Sets in",
                                  "Visible"], stretch=7)
        self.best = QCheckBox("Best first (highest passes)")
        self.radio_table = _table(["MHz", "Mode", "Baud", "Status", "What you can do", "Description"], stretch=5)
        self.listen_btn = QPushButton("Listen in Spectrum && receiver (with Doppler)")
        self.listen_btn.setEnabled(False)
        self.radio_note = QLabel("")
        self.radio_note.setWordWrap(True)
        self.sky = SkyPlot()
        self.about = QLabel("")
        self.about.setWordWrap(True)
        self.about.setTextFormat(Qt.TextFormat.RichText)
        self.about.setOpenExternalLinks(True)
        self.about.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.about.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.search)
        ll.addWidget(self.category)
        ll.addWidget(self.list, 1)
        passes_page = QWidget()
        pl = QVBoxLayout(passes_page)
        pl.addWidget(self.best)
        pl.addWidget(self.pass_table, 1)
        radio_page = QWidget()
        rl = QVBoxLayout(radio_page)
        rl.addWidget(self.radio_table, 1)
        rb = QHBoxLayout()
        rb.addWidget(self.listen_btn)
        rb.addWidget(self.radio_note, 1)
        rl.addLayout(rb)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.coming, "Coming over")
        self.tabs.addTab(passes_page, "Passes")
        self.tabs.addTab(radio_page, "Radio")
        self.tabs.addTab(self.sky, "Sky")
        self.tabs.addTab(self.about, "About")
        right = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.now_label)
        tl.addWidget(self.map, 1)
        right.addWidget(top)
        right.addWidget(self.tabs)
        right.setSizes([420, 360])
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([330, 900])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)

        self.search.textChanged.connect(lambda _: self.refresh_list())
        self.category.currentIndexChanged.connect(lambda _: (self.refresh_list(), self.refresh_coming()))
        self.list.itemSelectionChanged.connect(self._picked)
        for t in (self.list, self.coming):
            t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            t.customContextMenuRequested.connect(lambda pos, t=t: self._menu(t, pos))
        self.coming.itemSelectionChanged.connect(self._picked_coming)
        self.pass_table.itemSelectionChanged.connect(self._picked_pass)
        self.best.toggled.connect(lambda _: self._show_passes())
        self.radio_table.itemSelectionChanged.connect(self._picked_radio)
        self.radio_table.doubleClicked.connect(lambda _: self._listen())
        self.listen_btn.clicked.connect(self._listen)
        self.map.picked.connect(self._map_picked)
        self.map.cleared.connect(self.deselect)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)

    # ---- data in -----------------------------------------------------------------------

    def set_settings(self, home, min_el, days, only_hear):
        changed = (home, float(min_el), int(days)) != (self.home, self.min_el, self.days)
        self.home, self.min_el, self.days, self.only_hear = home, float(min_el), int(days), bool(only_hear)
        if changed and self.selected is not None:
            self.select(self.selected.norad)
        self.refresh_list()

    def set_catalogue(self, catalogue):
        self.catalogue = catalogue
        self.refresh_list()
        self.timer.start()

    def set_next(self, passes, done=True):
        """The next passes (again, as more are worked out). While they're coming in, the tables are
        refreshed at most every REFRESH s: each refresh of a thousand rows takes a moment."""
        self.next_passes = passes
        if not done and time.time() - self._refreshed < REFRESH:
            return
        self._refreshed = time.time()
        self.refresh_list()
        self.refresh_coming()

    def shown(self):
        """The satellites the list shows (category, search, can you hear it)."""
        cat, q = self.category.currentData(), self.search.text().strip().lower()
        out = []
        for s in self.catalogue.values():
            if self.only_hear and not s.can_hear:
                continue
            if cat and cat.startswith("has:"):
                if cat[4:] not in s.features:
                    continue
            elif cat and cat not in s.categories:
                continue
            if q and q not in s.name.lower() and q not in s.names.lower() and q != str(s.norad):
                continue
            out.append(s)
        return out

    def refresh_list(self):
        now = time.time()
        sats = sorted(self.shown(), key=lambda s: self._next(s, now)[0] if self._next(s, now) else 1e12)
        rows = []
        for s in sats:
            nxt = self._next(s, now)
            if nxt is None:
                pass_text, high = ("…" if not self.next_passes else "none in a day"), ""
            elif nxt[1].start <= now:
                pass_text, high = "up now", f"{nxt[1].max_el:.0f}°"
            else:
                pass_text, high = when(nxt[1].start, now), f"{nxt[1].max_el:.0f}°"
            rows.append([s.name, pass_text, high, ", ".join(c for c in s.categories if c != "Other")])
        _fill(self.list, rows, [s.norad for s in sats])
        for r in range(self.list.rowCount()):
            if self.selected is not None and _key(self.list, r) == self.selected.norad:
                reselect(self.list, r)
                break
        else:                                           # not listed now: no row stays lit for it
            sm = self.list.selectionModel()
            sm.blockSignals(True)
            self.list.clearSelection()
            sm.blockSignals(False)

    def _next(self, sat, now):
        for p in self.next_passes.get(sat.norad, []):
            if p.end > now and p.max_el >= self.min_el:
                return (p.start, p)
        return None

    def refresh_coming(self):
        now = time.time()
        shown = {s.norad: s for s in self.shown()}
        rows, keys = [], []
        items = sorted(((p, shown[n]) for n, ps in self.next_passes.items() if n in shown for p in ps
                        if p.end > now and p.start < now + COMING_HOURS * 3600 and p.max_el >= self.min_el),
                       key=lambda x: x[0].start)
        for p, s in items:
            radios = [r for r in s.radios if r.alive and r.kind in ("listen", "decode", "pictures")]
            what = ", ".join(dict.fromkeys(short(r) for r in radios[:4]))
            rows.append(["up now" if p.start <= now else when(p.start, now), s.name, f"{p.max_el:.0f}°",
                         span(p.duration), "yes" if p.visible else "", what])
            keys.append(s.norad)
        _fill(self.coming, rows, keys)

    # ---- one satellite --------------------------------------------------------------------

    def _picked(self):
        n = _key(self.list, self.list.currentRow())
        if n is not None:
            self.select(n)

    PURPOSES = (("beacon", "Listen for its beacon"), ("telemetry", "Listen for its telemetry (decode it)"),
                ("repeater", "Listen to its repeater / transponder output"))

    def _menu(self, table, pos):
        """Right-click a satellite: listen for its beacon, its telemetry or its repeater's output."""
        n = _key(table, table.rowAt(pos.y()))
        sat = self.catalogue.get(n) if n is not None else None
        if sat is None:
            return
        menu = QMenu(self)
        title = menu.addAction(sat.name)
        title.setEnabled(False)
        for purpose, label in self.PURPOSES:
            r = pick(sat, purpose)
            if r is None:
                a = menu.addAction(f"{label}: it has none")
                a.setEnabled(False)
                continue
            a = menu.addAction(f"{label}: {short(r)}" + ("" if r.alive else " (not heard lately)"))
            a.setToolTip(r.how)
            a.triggered.connect(lambda _=False, sat=sat, r=r: self.listen.emit(sat, r))
        menu.setToolTipsVisible(True)
        menu.exec(table.viewport().mapToGlobal(pos))

    def _map_picked(self, key):
        """A satellite clicked on the map: picked as if in the list (shown there, its tabs filled)."""
        key = str(key)
        if not key.startswith("sat") or not key[3:].isdigit():
            return
        n = int(key[3:])
        if self.selected is not None and self.selected.norad == n:
            return
        for r in range(self.list.rowCount()):
            if _key(self.list, r) == n:
                jump_to(self.list, r)
                break
        else:
            self.list.clearSelection()                  # not in the list as filtered: picked all the same
        self.select(n)

    def deselect(self):
        """A click on the map away from the satellite: nothing picked, every one that's up shown."""
        if self.selected is None:
            return
        self.selected, self._sat, self._passes, self._ground = None, None, [], {}
        sm = self.list.selectionModel()
        sm.blockSignals(True)
        self.list.clearSelection()
        sm.blockSignals(False)
        for t in (self.pass_table, self.radio_table):
            t.setRowCount(0)
        self._shown_passes = []
        self.sky.now = None
        self.sky.set_pass(None, None, "")
        self.about.setText("")
        self.radio_note.setText("")
        self.listen_btn.setEnabled(False)
        self.now_label.setText("Pick a satellite: its passes, radios and where it is")
        self.map.select(None)
        self.map.footprints = []
        self.map.set_points([], self.home)                 # (the ones up, if any, come next)
        self._show_listed()
        self.map.refit()

    def _picked_coming(self):
        n = _key(self.coming, self.coming.currentRow())
        if n is not None:
            self.select(n)

    def select(self, norad):
        from . import orbit
        sat = self.catalogue.get(norad)
        if sat is None:
            return
        self.selected = sat
        self._sat = orbit.satrec(sat.line1, sat.line2)
        self._passes = []
        if self.home is not None:
            try:
                self._passes = [p for p in orbit.passes(self._sat, self.home, time.time() - 1200, self.days,
                                                        self.min_el) if p.end > time.time()]
            except Exception:  # elements SGP4 can't use
                self._passes = []
        self._show_passes()
        self._show_radios()
        self._show_about()
        self._ground = {}
        self.tick()
        if self.map.follow:
            self.map.fit()

    def _show_passes(self):
        now = time.time()
        ps = sorted(self._passes, key=lambda p: -p.max_el) if self.best.isChecked() else list(self._passes)
        unsorted(self.pass_table)
        rows = [[when(p.start, now), when(p.peak, now), f"{p.max_el:.0f}° {compass(p.peak_az)}",
                 when(p.end, now), span(p.duration), "now" if p.start <= now else span(p.start - now),
                 span(p.end - now), "yes" if p.visible else ""] for p in ps]
        _fill(self.pass_table, rows, list(range(len(ps))))
        self._shown_passes = ps
        if not ps:
            self.sky.set_pass(None, None, "No passes" if self.home else "Set your location for passes")
        else:
            self.pass_table.selectRow(0)
            self._picked_pass()

    def _picked_pass(self):
        from . import orbit
        i = _key(self.pass_table, self.pass_table.currentRow())
        ps = getattr(self, "_shown_passes", [])
        if i is None or i >= len(ps) or self._sat is None:
            return
        p = ps[i]
        tr = orbit.track(self._sat, np.arange(p.start, p.end + 1, 5.0), self.home)
        self.sky.set_pass(tr.az, tr.el, f"{self.selected.name}  {when(p.start)}  highest {p.max_el:.0f}°")

    def _show_radios(self):
        sat = self.selected
        rows = []
        for r in sat.radios:
            f = f"{r.downlink / 1e6:.4f}" + (f"–{r.downlink_high / 1e6:.4f}" if r.downlink_high > r.downlink else "")
            if r.uplink:
                f += f" (up {r.uplink / 1e6:.3f})"
            rows.append([f, r.mode, f"{r.baud:g}" if r.baud else "", "heard" if r.alive else "not heard",
                         f"{KIND_NAMES.get(r.kind, r.kind)}: {r.how}", r.description])
        unsorted(self.radio_table)
        _fill(self.radio_table, rows, list(range(len(sat.radios))))
        heard = [r for r in sat.radios if r.alive and r.kind in ("listen", "decode")]
        self.radio_note.setText(f"{len(heard)} you can listen to or decode here" if heard else
                                "Nothing on it you can listen to or decode in the toolkit")
        self.listen_btn.setEnabled(False)
        for r, radio in enumerate(sat.radios):
            if radio.alive and radio.kind in ("listen", "decode"):
                self.radio_table.selectRow(r)
                break

    def _radio(self):
        i = _key(self.radio_table, self.radio_table.currentRow())
        return self.selected.radios[i] if self.selected is not None and i is not None else None

    def _picked_radio(self):
        r = self._radio()
        self.listen_btn.setEnabled(r is not None and bool(r.demod))

    def _listen(self):
        r = self._radio()
        if r is not None and r.demod:
            self.listen.emit(self.selected, r)

    def _show_about(self):
        s = self.selected
        rows = [("NORAD number", str(s.norad)), ("Also called", s.names), ("Operator", s.operator),
                ("Countries", s.countries), ("Launched", s.launched),
                ("Kind", ", ".join(s.categories)),
                ("Downlinks", f"{sum(r.alive for r in s.radios)} heard of {len(s.radios)} known")]
        text = f"<h3>{html.escape(s.name)}</h3><table>" + "".join(
            f"<tr><td><b>{html.escape(k)}</b>&nbsp;&nbsp;</td><td>{html.escape(v)}</td></tr>" for k, v in rows if v
        ) + "</table>"
        links = [(f"SatNOGS DB: {s.name}", f"https://db.satnogs.org/satellite/{s.norad}"),
                 ("N2YO", f"https://www.n2yo.com/satellite/?s={s.norad}")]
        if s.website:
            links.insert(0, ("Website", s.website))
        text += "<p>" + " · ".join(f"<a href='{html.escape(u)}'>{html.escape(t)}</a>" for t, u in links) + "</p>"
        text += ("<p style='color:gray'>Radios from the SatNOGS DB, the community's database of satellite "
                 "transmitters; orbits from its elements (Space-Track).</p>")
        self.about.setText(text)

    # ---- every second: where it is now -----------------------------------------------------

    def tick(self):
        from . import orbit
        if not self.isVisible():
            return
        sat = self.selected
        if sat is None or self._sat is None:
            self._show_listed()
            return
        now = time.time()
        here = self.home or (0.0, 0.0)
        tr = orbit.track(self._sat, [now], here)
        if not tr.ok[0]:
            self.now_label.setText(f"{sat.name}: no position (old elements?)")
            return
        lat, lon, alt = float(tr.lat[0]), float(tr.lon[0]), float(tr.alt[0])
        # The ground track: an orbit's worth around now, worked out again every minute.
        if not self._ground or now - self._ground["t"] > 60:
            period = 2 * math.pi / self._sat.no_kozai * 60 if self._sat.no_kozai else 5400
            ts = np.arange(now - period / 2, now + period, 30.0)
            g = orbit.track(self._sat, ts, here)
            self._ground = {"t": now, "track": [(float(a), float(b)) for a, b, ok in zip(g.lat, g.lon, g.ok) if ok]}
        text = f"{sat.name}: over {lat:.2f}°, {lon:.2f}° at {alt:,.0f} km"
        self.sky.now = None
        if self.home is not None:
            el, az = float(tr.el[0]), float(tr.az[0])
            if el > 0:
                self.sky.now = (az, el)
                text += f" · UP NOW: {el:.0f}° {compass(az)} ({az:.0f}°), {tr.range[0]:,.0f} km away"
                r = next((r for r in sat.radios if r.alive and r.kind in ("listen", "decode")), None)
                if r is not None:
                    shift = orbit.doppler(r.downlink, float(tr.rate[0])) - r.downlink
                    text += f" · {r.downlink / 1e6:.3f} MHz heard {shift / 1000:+.1f} kHz off"
            else:
                nxt = next((p for p in self._passes if p.start > now), None)
                if nxt is not None:
                    text += f" · next pass in {span(nxt.start - now)}, highest {nxt.max_el:.0f}°"
        self.now_label.setText(text)
        self.sky.update()
        self.map.footprints = [(lat, lon, orbit.footprint_km(alt))]
        self.map.set_points([{"id": f"sat{sat.norad}", "kind": "satellite", "lat": lat, "lon": lon,
                              "label": sat.name, "track": self._ground["track"]}], self.home)

    def _show_listed(self):
        """With nothing picked: every satellite in the list as it's filtered now, where it is on the map
        (the ones up over you marked "up")."""
        from . import orbit
        if not self.catalogue:
            return
        now = time.time()
        listed = sorted(self.shown(), key=lambda s: s.name)
        cache = self.__dict__.setdefault("_satrecs", {})
        pts, up = [], 0
        for s in listed[:MAX_ON_MAP]:
            rec = cache.get(s.norad)
            if rec is None or rec[0] != s.line1:
                rec = cache[s.norad] = (s.line1, orbit.satrec(s.line1, s.line2))
            tr = orbit.track(rec[1], [now], self.home or (0.0, 0.0))
            if not tr.ok[0]:
                continue
            is_up = self.home is not None and float(tr.el[0]) > 0
            up += is_up
            pts.append({"id": f"sat{s.norad}", "kind": "satellite", "lat": float(tr.lat[0]),
                        "lon": float(tr.lon[0]), "label": s.name + (" (up)" if is_up else ""), "track": []})
        self.map.footprints = []
        self.map.set_points(pts, self.home)
        more = f" (the first {MAX_ON_MAP} on the map)" if len(listed) > MAX_ON_MAP else ""
        text = f"{len(listed)} satellites in the list{more}" + (f", {up} up over you now" if self.home else "")
        self.now_label.setText(text + " · click one for its passes and radios")
