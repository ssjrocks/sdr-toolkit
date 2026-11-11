"""How far your station hears, in each direction: the ADS-B range rings.

Positions are sorted into 10° sectors around your location and 5 km distance bins.
For each sector:

- maximum range: the farthest position heard (from an aircraft heard at least twice,
  so a single garbled position doesn't count);
- reliable range: how far out an aircraft's position keeps arriving, at least 80% of
  updates within 10 s of the one before. Close in nearly every update is on time;
  towards the edge of reception they get patchy. It's the outer edge of the bins,
  counted from the centre, where that holds (bins with under 10 updates are skipped);
- estimated range: how far out aircraft still are while they're heard without a position.
  An aircraft flying away often goes on sending other messages (speed, altitude, its
  identity) after its last position, and one flying in is often heard before its first: from
  the nearest position and its ground speed and track, it's dead-reckoned forward to its last
  message, or back to its first. Only gaps of at least EST_GAP and at most MAX_DR count.

The counts are kept per location (rounded to about 1 km) in the logbook, so the
rings build up over days.
"""

import math

SECTORS = 36
BIN_KM = 5.0
ON_TIME = 10.0          # s: an update this soon after the last one is "on time"
RELIABLE = 0.8
MIN_UPDATES = 10
EST_GAP = 5.0           # s: a gap between messages and positions shorter than this isn't estimated
MAX_DR = 300.0          # s: dead reckoning past this is too rough (turns, climbs)
FORGET = 120.0          # s with nothing heard: an aircraft heard again starts afresh


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
        self.est_km = data.get("est_km", [0.0] * SECTORS)
        self._heard: dict[str, tuple] = {}       # key -> (first, last) message time of this visit
        self._fix: dict[str, tuple] = {}         # key -> (t, lat, lon, ground speed kt, track°): its last position
        self._entry: dict[str, tuple] = {}       # key -> (t, lat, lon) of its first position this visit
        self._last: dict[str, tuple] = {}        # key -> (t, lat, lon) of its last new position
        self._count: dict[str, int] = {}
        self._first: dict[str, tuple] = {}       # an aircraft's first position, counted once it's heard again

    def data(self) -> dict:
        return {"total": self.total, "on_time": self.on_time, "max_km": self.max_km, "est_km": self.est_km}

    def _bin(self, sector, i, table):
        row = table[sector]
        if len(row) <= i:
            row.extend([0] * (i + 1 - len(row)))
        row[i] += 1

    def heard(self, key, t) -> bool:
        """Any message from an aircraft (its latest at time t). While positions have stopped coming,
        it's dead-reckoned from its last one. Returns True if the estimated range grew."""
        first, last = self._heard.get(key, (None, None))
        if last is None or t - last > FORGET:
            self._heard[key] = (t, t)                  # a new visit
            for table in (self._fix, self._entry, self._last):
                table.pop(key, None)
            return False
        self._heard[key] = (first, max(last, t))
        fix = self._fix.get(key)
        if fix is None or not fix[3] or fix[4] is None or self._count.get(key, 0) < 2:
            return False
        dt = t - fix[0]
        if not EST_GAP <= dt <= MAX_DR:
            return False
        return self._estimate(fix[1:3], fix[4], fix[3], dt)

    def _estimate(self, pos, track, knots, seconds) -> bool:
        km = knots * 1.852 / 3600 * seconds
        b, d = bearing_km(self.home, destination(pos, track, km))
        if d > 800:
            return False
        s = int(b // (360 / SECTORS)) % SECTORS
        if d <= self.est_km[s]:
            return False
        self.est_km[s] = d
        return True

    def add(self, key, t, lat, lon, gs=None, track=None) -> bool:
        """A position update (at time t, with the aircraft's ground speed in knots and track if known);
        returns True if it changed anything."""
        if gs and track is not None:
            entry, heard = self._entry.get(key), self._heard.get(key)
            if entry is not None and heard is not None and self._count.get(key, 0) >= 2:
                # Heard before its first position: dead-reckon that position back to the first message.
                dt = entry[0] - heard[0]
                self._entry.pop(key)
                if EST_GAP <= dt <= MAX_DR:
                    self._estimate(entry[1:], (track + 180) % 360, gs, dt)
        last = self._last.get(key)
        if last and (last[1], last[2]) == (lat, lon):
            if gs and track is not None and key in self._fix:
                self._fix[key] = self._fix[key][:3] + (gs, track)
            return False
        self._fix[key] = (t, lat, lon, gs, track)
        if key in self._heard and key not in self._entry and last is None:
            self._entry[key] = (t, lat, lon)
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
        if last is not None and t - last[0] <= ON_TIME:
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
        """(maximum, reliable, estimated): each a list of (bearing, km) per sector, or None if nothing yet."""
        step = 360 / SECTORS
        mx = [(s * step + step / 2, self.max_km[s]) for s in range(SECTORS)]
        rel = [(s * step + step / 2, self.reliable_km(s)) for s in range(SECTORS)]
        est = [(s * step + step / 2, max(self.est_km[s], self.max_km[s])) for s in range(SECTORS)]
        return (mx if any(k for _, k in mx) else None, rel if any(k for _, k in rel) else None,
                est if any(self.est_km) else None)

    def summary(self) -> str:
        mx, rel, est = self.outlines()
        if not mx:
            return ""
        far = max(mx, key=lambda x: x[1])
        text = f"furthest {far[1]:.0f} km at {far[0]:.0f}°"
        if rel:
            r = [k for _, k in rel if k]
            text += f", reliable to {sum(r) / len(r):.0f} km on average"
        if est:
            text += f", heard to about {max(k for _, k in est):.0f} km (estimated)"
        return text
