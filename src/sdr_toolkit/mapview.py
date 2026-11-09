"""A slippy map of OpenStreetMap tiles with aircraft / ship markers.

Tiles follow OSM's tile usage policy: an identifying User-Agent, at most two
downloads at a time, and a disk cache (~/.cache/sdr-toolkit/tiles) that's
reused for 30 days. The attribution is drawn on the map.
"""

import math
import time
from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QUrl
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QToolButton, QWidget

from . import __version__

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
    def __init__(self, cache_dir: Path | None = None, online=True):
        super().__init__()
        self.setMinimumHeight(200)
        self.cache_dir = cache_dir or Path.home() / ".cache/sdr-toolkit/tiles"
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
        self._buttons = []
        for text, tip, fn in (("+", "Zoom in", lambda: self._zoom_by(1)), ("−", "Zoom out", lambda: self._zoom_by(-1)),
                              ("Fit", "Show everything (and keep following)", self.refit)):
            b = QToolButton(self)
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            self._buttons.append(b)

    # ---- data ----------------------------------------------------------

    def set_points(self, points, home=None):
        self.points = points
        self.home = home
        if self.follow:
            self.fit()
        self.update()

    def refit(self):
        self.follow = True
        self.fit()
        self.update()

    def fit(self):
        coords = [(p["lat"], p["lon"]) for p in self.points]
        if self.home:
            coords.append(self.home)
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

    def resizeEvent(self, ev):
        x = self.width() - 8
        for b in reversed(self._buttons):
            b.adjustSize()
            x -= b.width()
            b.move(x, 8)
            x -= 4
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

    def _draw_points(self, p):
        if self.home:
            c = self._screen(*self.home)
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.setBrush(QColor(30, 110, 255))
            p.drawEllipse(c, 6, 6)
        for pt in self.points:
            colour = QColor(220, 60, 40) if pt.get("kind") == "aircraft" else QColor(20, 120, 60)
            track = pt.get("track") or []
            if len(track) > 1:
                p.setPen(QPen(QColor(colour.red(), colour.green(), colour.blue(), 140), 2))
                p.drawPolyline(QPolygonF([self._screen(a, b) for a, b in track]))
            c = self._screen(pt["lat"], pt["lon"])
            p.save()
            p.translate(c)
            if pt.get("heading") is not None:
                p.rotate(float(pt["heading"]))
            p.setPen(QPen(QColor(255, 255, 255), 1.5))
            p.setBrush(QBrush(colour))
            if pt.get("kind") == "aircraft":
                shape = QPolygonF([QPointF(0, -10), QPointF(7, 8), QPointF(0, 4), QPointF(-7, 8)])
                p.drawPolygon(shape)
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

    def mouseDoubleClickEvent(self, ev):
        self.refit()
