"""A slippy map of OpenStreetMap tiles with aircraft / ship markers.

It can also show every track of the session (craft no longer heard stay, dimmed,
where they were last heard) and the ADS-B range rings (see coverage.py). Clicking
a marker emits ``picked`` with its id.

Tiles follow OSM's tile usage policy: an identifying User-Agent, at most two
downloads at a time, and a disk cache (~/.cache/sdr-toolkit/tiles) that's
reused for 30 days. The attribution is drawn on the map.
"""

import math
import time
from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QToolButton, QWidget

from . import __version__
from .coverage import destination
from . import system

# Markers move on between reports: from the last position along its course at its speed (dead
# reckoning), drawn ten times a second. After OVERDUE of its usual updates are missed, the
# position is a prediction: the icon flashes, with a dashed line back to the last real position.
FRAME_MS = 100
OVERDUE = 3                     # missed updates, at the track's usual interval...
MIN_OVERDUE = 5.0               # ...and at least this many seconds
DROP_AIRCRAFT = 60.0            # s with no message at all: an aircraft leaves the map
MAX_COAST = {"aircraft": DROP_AIRCRAFT, "ship": 600.0}    # s of dead reckoning at most
USUAL = {"aircraft": 1.0, "ship": 30.0}                   # update interval to assume at first

TILE = 256
URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = f"SDR-Toolkit/{__version__} (+https://github.com/ssjrocks/sdr-toolkit)"
MAX_AGE = 30 * 24 * 3600
MIN_ZOOM, MAX_ZOOM = 2, 17


def to_world(lat, lon, zoom) -> tuple[float, float]:
    """Web Mercator pixel position at *zoom*."""
    n = TILE * 2**zoom
    lat = max(min(lat, 85.0511), -85.0511)
    x = (lon + 180.0) / 360.0 * n
    r = math.radians(lat)
    y = (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n
    return x, y


def from_world(x, y, zoom) -> tuple[float, float]:
    n = TILE * 2**zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


class MapView(QWidget):
    picked = pyqtSignal(object)          # a marker's id

    def __init__(self, cache_dir: Path | None = None, online=True):
        super().__init__()
        self.setMinimumHeight(200)
        self.cache_dir = cache_dir or system.cache_dir() / "tiles"
        self.online = online
        self.zoom = 3
        self.center = (0.0, 0.0)
        self.points: list[dict] = []
        self.home = None
        self.follow = True              # keep everything in view until the user pans or zooms
        self._tiles: OrderedDict = OrderedDict()
        self._failed: dict = {}
        self._queue: list = []
        self._active = 0
        self._drag = None
        self._net = QNetworkAccessManager(self)
        self.setMouseTracking(False)
        self.setToolTip("Drag to move, wheel to zoom. Fit shows everything again.")
        self.session: dict = {}          # id -> {"track": [...], "last": point}: everything this session
        self._usual: dict = {}           # id -> (last position time, its usual update interval)
        self.footprints: list = []       # (lat, lon, radius km): a satellite's coverage
        self._anim = QTimer(self)
        self._anim.setInterval(FRAME_MS)
        self._anim.timeout.connect(self._animate)
        self.show_session = False
        self.keep_session = True         # remember every track this session (not for satellites)
        self.rings = None                # (maximum, reliable, estimated) outlines: [(bearing, km)] each
        self.show_rings = True
        self.selected = None
        self._buttons = []
        for text, tip, fn in (("+", "Zoom in", lambda: self._zoom_by(1)), ("−", "Zoom out", lambda: self._zoom_by(-1)),
                              ("Fit", "Show everything (and keep following)", self.refit)):
            b = QToolButton(self)
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            self._buttons.append(b)
        self.tracks_btn = QToolButton(self)
        self.tracks_btn.setText("All tracks")
        self.tracks_btn.setCheckable(True)
        self.tracks_btn.setToolTip("Every track heard this session; craft no longer heard stay where they were "
                                   "last heard, dimmed")
        self.tracks_btn.toggled.connect(self._set_show_session)
        self.rings_btn = QToolButton(self)
        self.rings_btn.setText("Range")
        self.rings_btn.setCheckable(True)
        self.rings_btn.setChecked(True)
        self.rings_btn.setToolTip("Range rings: how far reception is reliable (green), the furthest positions "
                                  "heard (red) and, estimated, where aircraft were still heard (purple)")
        self.rings_btn.toggled.connect(self._set_show_rings)
        self.rings_btn.setVisible(False)
        self._buttons = [self.rings_btn, self.tracks_btn] + self._buttons

    # ---- data ----------------------------------------------------------

    def set_points(self, points, home=None):
        self.points = points
        self.home = home
        moving = False
        for p in points if self.keep_session else ():
            t = p.get("pos_t")
            if t:
                last, usual = self._usual.get(p["id"], (None, None))
                if last is not None and t > last:
                    gap = min(t - last, 600.0)
                    usual = gap if usual is None else 0.7 * usual + 0.3 * gap
                self._usual[p["id"]] = (t, usual) if last is None or t >= last else (last, usual)
                moving = moving or bool(p.get("gs"))
            if p["id"] not in self.session:          # start from the track it already has
                self.session[p["id"]] = {"track": list(p.get("track") or [])}
            entry = self.session[p["id"]]
            track = entry["track"]
            if not track or track[-1] != (p["lat"], p["lon"]):
                track.append((p["lat"], p["lon"]))
                del track[:-5000]
            entry["last"] = p
        if moving and not self._anim.isActive():
            self._anim.start()
        if self.follow:
            self.fit()
        self.update()

    def live(self, pt, now=None):
        """Where a point is now: moved on from its last position at its speed along its course, and
        whether that's a prediction (its updates are overdue). Unchanged without speed and course."""
        now = time.time() if now is None else now
        pos_t, gs = pt.get("pos_t"), pt.get("gs")
        course = pt.get("course", pt.get("heading"))
        if not pos_t or not gs or course is None:
            return pt
        kind = pt.get("kind", "aircraft")
        since = max(now - pos_t, 0.0)
        usual = self._usual.get(pt["id"], (None, None))[1] or USUAL.get(kind, 5.0)
        km = float(gs) * 1.852 / 3600 * min(since, MAX_COAST.get(kind, DROP_AIRCRAFT))
        lat, lon = destination((pt["lat"], pt["lon"]), float(course), km)
        return dict(pt, lat=lat, lon=lon, fix=(pt["lat"], pt["lon"]),
                    predicted=since > max(OVERDUE * usual, MIN_OVERDUE))

    def current(self, now=None):
        """The points still on the map: an aircraft goes DROP_AIRCRAFT after its last message."""
        now = time.time() if now is None else now
        return [pt for pt in self.points if not (pt.get("kind") == "aircraft" and pt.get("msg_t")
                                                 and now - pt["msg_t"] > DROP_AIRCRAFT)]

    def _animate(self):
        if any(pt.get("gs") and pt.get("pos_t") for pt in self.current()):
            self.update()
        else:
            self._anim.stop()

    def clear_session(self):
        self.session = {}
        self._usual = {}
        self.selected = None
        self.set_rings(None)

    def set_rings(self, outlines):
        first = self.rings is None
        self.rings = outlines if outlines and any(outlines) else None
        self.rings_btn.setVisible(self.rings is not None)
        self._place_buttons()
        if first and self.rings and self.follow:
            self.fit()
        self.update()

    def _set_show_session(self, on):
        self.show_session = on
        self.update()

    def _set_show_rings(self, on):
        self.show_rings = on
        self.update()

    def select(self, key):
        self.selected = key
        self.update()

    def refit(self):
        self.follow = True
        self.fit()
        self.update()

    def fit(self):
        coords = [(p["lat"], p["lon"]) for p in self.points]
        if self.home:
            coords.append(self.home)
            if self.rings and self.show_rings:
                for outline in (self.rings[0], self.rings[2] if len(self.rings) > 2 else None):
                    coords += [destination(self.home, b, km) for b, km in outline or () if km]
        if not coords:
            return
        if len(coords) == 1:
            self.center, self.zoom = coords[0], 10
            return
        lats, lons = [c[0] for c in coords], [c[1] for c in coords]
        w, h = max(self.width() - 80, 100), max(self.height() - 80, 100)
        for z in range(13, MIN_ZOOM - 1, -1):
            x0, y0 = to_world(max(lats), min(lons), z)
            x1, y1 = to_world(min(lats), max(lons), z)
            if x1 - x0 <= w and y1 - y0 <= h:
                break
        self.zoom = z
        self.center = from_world((x0 + x1) / 2, (y0 + y1) / 2, z)

    # ---- tiles ---------------------------------------------------------

    def _tile_path(self, z, x, y) -> Path:
        return self.cache_dir / str(z) / str(x) / f"{y}.png"

    def _tile(self, z, x, y):
        key = (z, x, y)
        if key in self._tiles:
            self._tiles.move_to_end(key)
            return self._tiles[key]
        path = self._tile_path(z, x, y)
        if path.is_file():
            pix = QPixmap(str(path))
            if not pix.isNull():
                self._remember(key, pix)
                if time.time() - path.stat().st_mtime < MAX_AGE:
                    return pix
        if self.online and key not in self._queue and time.time() - self._failed.get(key, 0) > 60:
            self._queue.append(key)
            self._pump()
        return self._tiles.get(key)

    def _remember(self, key, pix):
        self._tiles[key] = pix
        while len(self._tiles) > 400:
            self._tiles.popitem(last=False)

    def _pump(self):
        while self._active < 2 and self._queue:
            z, x, y = self._queue.pop()               # newest first: what's on screen now
            req = QNetworkRequest(QUrl(URL.format(z=z, x=x, y=y)))
            req.setRawHeader(b"User-Agent", USER_AGENT.encode())
            reply = self._net.get(req)
            self._active += 1
            reply.finished.connect(lambda r=reply, k=(z, x, y): self._got(r, k))

    def _got(self, reply, key):
        self._active -= 1
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = bytes(reply.readAll())
                pix = QPixmap()
                if pix.loadFromData(data):
                    path = self._tile_path(*key)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    self._remember(key, pix)
                    self.update()
            else:
                self._failed[key] = time.time()
        finally:
            reply.deleteLater()
            self._pump()

    # ---- drawing -------------------------------------------------------

    def _origin(self):
        cx, cy = to_world(*self.center, self.zoom)
        return cx - self.width() / 2, cy - self.height() / 2

    def _screen(self, lat, lon):
        ox, oy = self._origin()
        x, y = to_world(lat, lon, self.zoom)
        return QPointF(x - ox, y - oy)

    def _place_buttons(self):
        x = self.width() - 8
        for b in reversed(self._buttons):
            if b.isHidden() and b in (self.rings_btn, self.tracks_btn):
                continue
            b.adjustSize()
            x -= b.width()
            b.move(x, 8)
            x -= 4

    def resizeEvent(self, ev):
        self._place_buttons()
        if self.follow:
            self.fit()
        super().resizeEvent(ev)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(170, 211, 223))
        ox, oy = self._origin()
        n = 2**self.zoom
        tx0, ty0 = int(ox // TILE), int(oy // TILE)
        for tx in range(tx0, int((ox + self.width()) // TILE) + 1):
            for ty in range(max(ty0, 0), min(int((oy + self.height()) // TILE) + 1, n)):
                pix = self._tile(self.zoom, tx % n, ty)
                if pix is not None:
                    p.drawPixmap(int(tx * TILE - ox), int(ty * TILE - oy), pix)
        self._draw_points(p)
        # Attribution, as OSM requires.
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 0.85)
        p.setFont(f)
        text = "© OpenStreetMap contributors"
        r = p.fontMetrics().boundingRect(text)
        box = QRectF(self.width() - r.width() - 10, self.height() - r.height() - 6, r.width() + 8, r.height() + 4)
        p.fillRect(box, QColor(255, 255, 255, 200))
        p.setPen(QColor(40, 40, 40))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    def _draw_rings(self, p):
        if not (self.rings and self.show_rings and self.home):
            return
        maximum, reliable = self.rings[:2]
        estimated = self.rings[2] if len(self.rings) > 2 else None
        # Top first: green over red over purple, each drawn only where the ones above it aren't.
        layers = ((reliable, QColor(20, 150, 60, 220), Qt.PenStyle.SolidLine),
                  (maximum, QColor(200, 40, 40, 200), Qt.PenStyle.DashLine),
                  (estimated, QColor(140, 60, 200, 200), Qt.PenStyle.DotLine))
        whole = QPainterPath()
        whole.addRect(QRectF(self.rect()).adjusted(-20, -20, 20, 20))
        covered = QPainterPath()
        for outline, colour, style in layers:
            if not outline or not any(km for _, km in outline):
                continue
            area = QPainterPath()
            area.addPolygon(QPolygonF([self._screen(*destination(self.home, b, km)) for b, km in outline]))
            area.closeSubpath()
            p.save()
            if not covered.isEmpty():
                p.setClipPath(whole.subtracted(covered))
            pen = QPen(colour, 2)
            pen.setStyle(style)
            p.setPen(pen)
            p.setBrush(QColor(colour.red(), colour.green(), colour.blue(), 30))
            p.drawPath(area)
            p.restore()
            covered = covered.united(area)
        text = "green: reliable · red dashed: furthest position · purple dotted: heard, estimated"
        r = p.fontMetrics().boundingRect(text)
        box = QRectF(6, self.height() - r.height() - 6, r.width() + 8, r.height() + 4)
        p.fillRect(box, QColor(255, 255, 255, 200))
        p.setPen(QColor(40, 40, 40))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _lines(self, coords):
        """Screen lines through (lat, lon)s, broken where they cross the date line (the map repeats
        east-west, so a jump of over half a world is the other side of it)."""
        world = TILE * 2 ** self.zoom
        runs, run, last = [], [], None
        for a, b in coords:
            q = self._screen(a, b)
            if last is not None and abs(q.x() - last.x()) > world / 2:
                runs.append(run)
                run = []
            run.append(q)
            last = q
        runs.append(run)
        return [QPolygonF(r) for r in runs if len(r) > 1]

    def _draw_footprints(self, p):
        for lat, lon, km in self.footprints:
            ring = [destination((lat, lon), b, km) for b in range(0, 361, 4)]
            lines = self._lines(ring)
            p.setPen(QPen(QColor(255, 190, 40, 200), 1.5))
            if len(lines) == 1:
                p.setBrush(QColor(255, 190, 40, 35))
                p.drawPolygon(lines[0])
            else:
                for line in lines:
                    p.drawPolyline(line)

    def _draw_points(self, p):
        self._draw_rings(p)
        self._draw_footprints(p)
        if self.home:
            c = self._screen(*self.home)
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.setBrush(QColor(30, 110, 255))
            p.drawEllipse(c, 6, 6)
        now = time.time()
        shown = self.current(now)
        current = {pt["id"] for pt in shown}
        if self.show_session:
            for key, entry in self.session.items():
                if key not in current and "last" in entry:
                    self._draw_one(p, dict(entry["last"], track=entry["track"]), faded=True)
        for pt in shown:
            if self.show_session and pt["id"] in self.session:
                pt = dict(pt, track=self.session[pt["id"]]["track"])
            self._draw_one(p, self.live(pt, now), now=now)

    def _draw_one(self, p, pt, faded=False, now=0.0):
        colour = {"aircraft": QColor(220, 60, 40), "satellite": QColor(250, 170, 20)}.get(pt.get("kind"),
                                                                                       QColor(20, 120, 60))
        if faded:
            colour = QColor(colour.red(), colour.green(), colour.blue(), 110)
        track = pt.get("track") or []
        if len(track) > 1:
            p.setPen(QPen(QColor(colour.red(), colour.green(), colour.blue(), 70 if faded else 140), 2))
            for line in self._lines(track):
                p.drawPolyline(line)
        c = self._screen(pt["lat"], pt["lon"])
        if pt.get("fix") and pt.get("predicted"):
            # A predicted position: a dashed line back to the last real one, and the icon flashes.
            pen = QPen(QColor(colour.red(), colour.green(), colour.blue(), 170), 1.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(self._screen(*pt["fix"]), c)
        flash_off = pt.get("predicted") and int(now * 2) % 2 == 1
        if pt["id"] == self.selected:
            p.setPen(QPen(QColor(255, 200, 0), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, 15, 15)
        p.save()
        p.translate(c)
        if flash_off:
            p.setOpacity(0.15)
        if pt.get("heading") is not None:
            p.rotate(float(pt["heading"]))
        p.setPen(QPen(QColor(255, 255, 255, 120 if faded else 255), 1.5))
        p.setBrush(QBrush(colour))
        if pt.get("kind") == "aircraft":
            shape = QPolygonF([QPointF(0, -10), QPointF(7, 8), QPointF(0, 4), QPointF(-7, 8)])
            p.drawPolygon(shape)
        elif pt.get("kind") == "satellite":
            p.drawRect(QRectF(-4, -4, 8, 8))                 # the body, and its solar panels
            p.setBrush(QBrush(QColor(60, 110, 200)))
            p.drawRect(QRectF(-14, -3, 8, 6))
            p.drawRect(QRectF(6, -3, 8, 6))
        else:
            path = QPainterPath()
            path.moveTo(0, -10)
            path.lineTo(5, -2)
            path.lineTo(5, 8)
            path.lineTo(-5, 8)
            path.lineTo(-5, -2)
            path.closeSubpath()
            p.drawPath(path)
        p.restore()
        label = str(pt.get("label", ""))
        if label:
            r = p.fontMetrics().boundingRect(label)
            box = QRectF(c.x() + 10, c.y() - r.height() / 2 - 2, r.width() + 6, r.height() + 4)
            p.fillRect(box, QColor(255, 255, 255, 190))
            p.setPen(QColor(20, 20, 20))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    # ---- interaction ---------------------------------------------------

    def _zoom_by(self, dz, at=None):
        z = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom + dz))
        if z == self.zoom:
            return
        self.follow = False
        if at is None:
            at = QPointF(self.width() / 2, self.height() / 2)
        ox, oy = self._origin()
        lat, lon = from_world(ox + at.x(), oy + at.y(), self.zoom)      # keep this spot under the cursor
        self.zoom = z
        wx, wy = to_world(lat, lon, z)
        self.center = from_world(wx - at.x() + self.width() / 2, wy - at.y() + self.height() / 2, z)
        self.update()

    def wheelEvent(self, ev):
        self._zoom_by(1 if ev.angleDelta().y() > 0 else -1, ev.position())

    def mousePressEvent(self, ev):
        self._drag = (ev.position(), to_world(*self.center, self.zoom))
        self._press = ev.position()

    def point_at(self, pos, radius=14):
        """The id of the marker nearest *pos* (screen), if within *radius* pixels."""
        now = time.time()
        candidates = [self.live(pt, now) for pt in self.current(now)]
        if self.show_session:
            current = {pt["id"] for pt in candidates}
            candidates += [e["last"] for k, e in self.session.items() if k not in current and "last" in e]
        best, best_d = None, radius
        for pt in candidates:
            c = self._screen(pt["lat"], pt["lon"])
            d = ((c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2) ** 0.5
            if d <= best_d:
                best, best_d = pt["id"], d
        return best

    def mouseMoveEvent(self, ev):
        if self._drag is None:
            return
        start, (cx, cy) = self._drag
        d = ev.position() - start
        self.follow = False
        self.center = from_world(cx - d.x(), cy - d.y(), self.zoom)
        self.update()

    def mouseReleaseEvent(self, ev):
        self._drag = None
        press = getattr(self, "_press", None)
        if press is not None and (ev.position() - press).manhattanLength() < 5:
            key = self.point_at(ev.position())
            if key is not None:
                self.select(key)
                self.picked.emit(key)

    def mouseDoubleClickEvent(self, ev):
        self.refit()
