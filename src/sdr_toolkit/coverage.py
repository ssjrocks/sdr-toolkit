"""How far your station hears, in each direction: the ADS-B range rings.

Positions are sorted into 10° sectors around your location and 5 km distance bins.
For each sector:

- maximum range: the farthest position heard (from an aircraft heard at least twice,
  so a single garbled position doesn't count);
- reliable range: how far out an aircraft's position keeps arriving, at least 80% of
  updates within 10 s of the one before. Close in nearly every update is on time;
  towards the edge of reception they get patchy. It's the outer edge of the bins,
  counted from the centre, where that holds (bins with under 10 updates are skipped).

The counts are kept per location (rounded to about 1 km) in the logbook, so the
rings build up over days.
"""

import math

SECTORS = 36
BIN_KM = 5.0
ON_TIME = 10.0          # s: an update this soon after the last one is "on time"
RELIABLE = 0.8
MIN_UPDATES = 10


def bearing_km(home, pos) -> tuple[float, float]:
    (la1, lo1), (la2, lo2) = (math.radians(x) for x in home), (math.radians(x) for x in pos)
    dl = lo2 - lo1
    y = math.sin(dl) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    b = (math.degrees(math.atan2(y, x)) + 360) % 360
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dl / 2) ** 2
    return b, 6371.0 * 2 * math.asin(math.sqrt(min(1.0, h)))


def destination(home, bearing, km) -> tuple[float, float]:
    la1, lo1 = math.radians(home[0]), math.radians(home[1])
    d, b = km / 6371.0, math.radians(bearing)
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = lo1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return math.degrees(la2), (math.degrees(lo2) + 540) % 360 - 180


def site_key(home) -> str:
    return f"{home[0]:.2f},{home[1]:.2f}"


class Coverage:
    def __init__(self, home, data: dict | None = None):
        self.home = home
        data = data or {}
        self.total = data.get("total", [[] for _ in range(SECTORS)])
        self.on_time = data.get("on_time", [[] for _ in range(SECTORS)])
        self.max_km = data.get("max_km", [0.0] * SECTORS)
        self._last: dict[str, tuple] = {}        # key -> (t, lat, lon) of its last new position
        self._count: dict[str, int] = {}
        self._first: dict[str, tuple] = {}       # an aircraft's first position, counted once it's heard again

    def data(self) -> dict:
        return {"total": self.total, "on_time": self.on_time, "max_km": self.max_km}

    def _bin(self, sector, i, table):
        row = table[sector]
        if len(row) <= i:
            row.extend([0] * (i + 1 - len(row)))
        row[i] += 1

    def add(self, key, t, lat, lon) -> bool:
        """A position update; returns True if it changed anything."""
        last = self._last.get(key)
        if last and (last[1], last[2]) == (lat, lon):
            return False
        self._last[key] = (t, lat, lon)
        b, km = bearing_km(self.home, (lat, lon))
        if km > 800:
            return False                           # beyond any 1090 MHz horizon: a bad position
        s = int(b // (360 / SECTORS)) % SECTORS
        self._count[key] = self._count.get(key, 0) + 1
        if self._count[key] == 1:
            self._first[key] = (s, km)
            return False
        if self._count[key] == 2:
            fs, fkm = self._first.pop(key)
            self.max_km[fs] = max(self.max_km[fs], fkm)
        self.max_km[s] = max(self.max_km[s], km)
        i = int(km // BIN_KM)
        self._bin(s, i, self.total)
        if t - last[0] <= ON_TIME:
            self._bin(s, i, self.on_time)
        return True

    def reliable_km(self, sector) -> float:
        total, ok = self.total[sector], self.on_time[sector]
        edge = 0.0
        for i, n in enumerate(total):
            if n < MIN_UPDATES:
                continue
            good = ok[i] if i < len(ok) else 0
            if good / n < RELIABLE:
                break
            edge = (i + 1) * BIN_KM
        return edge

    def outlines(self):
        """(maximum, reliable): each a list of (bearing, km) per sector, or None if nothing yet."""
        step = 360 / SECTORS
        mx = [(s * step + step / 2, self.max_km[s]) for s in range(SECTORS)]
        rel = [(s * step + step / 2, self.reliable_km(s)) for s in range(SECTORS)]
        return (mx if any(k for _, k in mx) else None, rel if any(k for _, k in rel) else None)

    def summary(self) -> str:
        mx, rel = self.outlines()
        if not mx:
            return ""
        far = max(mx, key=lambda x: x[1])
        text = f"furthest {far[1]:.0f} km at {far[0]:.0f}°"
        if rel:
            r = [k for _, k in rel if k]
            text += f", reliable to {sum(r) / len(r):.0f} km on average"
        return text
