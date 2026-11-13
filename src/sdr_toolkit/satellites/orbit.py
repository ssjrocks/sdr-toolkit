"""Where a satellite is, when it passes over you, and its Doppler shift.

SGP4 (the sgp4 package) gives the satellite's position and velocity from its two-line elements, in
the TEME frame; turned by Greenwich sidereal time into Earth-fixed coordinates, that gives the
point below it, and from your location its azimuth, elevation, range and range rate. Polar motion
and nutation are left out: tens of metres, far below what tracking or Doppler needs.
"""

import math
from dataclasses import dataclass, field

import numpy as np
from sgp4.api import Satrec

EARTH_A = 6378.137                 # km, WGS84
EARTH_F = 1 / 298.257223563
EARTH_E2 = EARTH_F * (2 - EARTH_F)
OMEGA = 7.2921150e-5               # rad/s, Earth's rotation
C_KM_S = 299792.458
UNIX_JD = 2440587.5                # the Julian date of 1970-01-01


def satrec(line1, line2):
    return Satrec.twoline2rv(line1, line2)


def _jd(t):
    """Unix times -> (whole Julian day, fraction), for sgp4."""
    t = np.atleast_1d(np.asarray(t, float))
    jd = UNIX_JD + t / 86400.0
    whole = np.floor(jd - 0.5) + 0.5
    return whole, jd - whole


def gmst(t):
    """Greenwich mean sidereal time (rad) at unix times t (IAU 1982)."""
    jd = UNIX_JD + np.asarray(t, float) / 86400.0
    tu = (jd - 2451545.0) / 36525.0
    s = (67310.54841 + (876600.0 * 3600 + 8640184.812866) * tu + 0.093104 * tu ** 2 - 6.2e-6 * tu ** 3)
    return np.radians((s % 86400.0) / 240.0)


def observer_ecef(lat, lon, alt_km=0.0):
    la, lo = math.radians(lat), math.radians(lon)
    n = EARTH_A / math.sqrt(1 - EARTH_E2 * math.sin(la) ** 2)
    return np.array([(n + alt_km) * math.cos(la) * math.cos(lo), (n + alt_km) * math.cos(la) * math.sin(lo),
                     (n * (1 - EARTH_E2) + alt_km) * math.sin(la)])


def ecef_to_geodetic(r):
    """(N, 3) Earth-fixed km -> lat, lon (degrees), height (km), Bowring's method."""
    x, y, z = r[:, 0], r[:, 1], r[:, 2]
    p = np.hypot(x, y)
    b = EARTH_A * (1 - EARTH_F)
    ep2 = (EARTH_A ** 2 - b ** 2) / b ** 2
    th = np.arctan2(z * EARTH_A, p * b)
    lat = np.arctan2(z + ep2 * b * np.sin(th) ** 3, p - EARTH_E2 * EARTH_A * np.cos(th) ** 3)
    n = EARTH_A / np.sqrt(1 - EARTH_E2 * np.sin(lat) ** 2)
    h = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(np.arctan2(y, x)), h


@dataclass
class Track:
    """A satellite at a series of times, seen from one place."""
    t: np.ndarray
    lat: np.ndarray                # the point below it
    lon: np.ndarray
    alt: np.ndarray                # km
    az: np.ndarray                 # degrees from north
    el: np.ndarray                 # degrees above the horizon
    range: np.ndarray              # km
    rate: np.ndarray               # km/s, positive moving away
    ok: np.ndarray                 # SGP4 gave a position (decayed elements don't)
    ecef: np.ndarray = field(repr=False, default=None)


def track(sat, times, home):
    """Position, and position relative to `home` (lat, lon[, alt km]), at unix `times`."""
    t = np.atleast_1d(np.asarray(times, float))
    jd, fr = _jd(t)
    err, r, v = sat.sgp4_array(jd, fr)
    ok = err == 0
    th = gmst(t)
    c, s = np.cos(th), np.sin(th)
    # TEME -> Earth-fixed: turn by sidereal time; velocity also loses the Earth's rotation.
    x = c * r[:, 0] + s * r[:, 1]
    y = -s * r[:, 0] + c * r[:, 1]
    rf = np.stack([x, y, r[:, 2]], axis=1)
    vx = c * v[:, 0] + s * v[:, 1] + OMEGA * y
    vy = -s * v[:, 0] + c * v[:, 1] - OMEGA * x
    vf = np.stack([vx, vy, v[:, 2]], axis=1)
    lat, lon, alt = ecef_to_geodetic(rf)
    o = observer_ecef(home[0], home[1], home[2] if len(home) > 2 else 0.0)
    rho = rf - o
    la, lo = math.radians(home[0]), math.radians(home[1])
    east = np.array([-math.sin(lo), math.cos(lo), 0.0])
    north = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    e, n, u = rho @ east, rho @ north, rho @ up
    rng = np.linalg.norm(rho, axis=1)
    az = (np.degrees(np.arctan2(e, n)) + 360) % 360
    el = np.degrees(np.arcsin(np.clip(u / np.maximum(rng, 1e-9), -1, 1)))
    rate = np.einsum("ij,ij->i", rho, vf) / np.maximum(rng, 1e-9)
    return Track(t, lat, lon, alt, az, el, rng, rate, ok, rf)


def ground(sat, times):
    """Just the ground track: (lat, lon, ok) arrays at unix `times` (no observer: quicker than track)."""
    t = np.atleast_1d(np.asarray(times, float))
    jd, fr = _jd(t)
    err, r, _v = sat.sgp4_array(jd, fr)
    th = gmst(t)
    c, s = np.cos(th), np.sin(th)
    rf = np.stack([c * r[:, 0] + s * r[:, 1], -s * r[:, 0] + c * r[:, 1], r[:, 2]], axis=1)
    lat, lon, _alt = ecef_to_geodetic(rf)
    return lat, lon, err == 0


def doppler(freq_hz, rate_km_s):
    """The frequency heard from a transmitter on freq_hz moving away at rate_km_s."""
    return freq_hz * (1 - rate_km_s / C_KM_S)


# ---- the Sun: daylight, and when a satellite is lit (visible to the eye) ----------------------

def sun_ecef(t):
    """The Sun's direction, Earth-fixed unit vectors (the low-precision almanac formulas)."""
    t = np.atleast_1d(np.asarray(t, float))
    n = UNIX_JD + t / 86400.0 - 2451545.0
    g = np.radians((357.529 + 0.98560028 * n) % 360)
    q = np.radians((280.459 + 0.98564736 * n) % 360)
    lam = q + np.radians(1.915) * np.sin(g) + np.radians(0.020) * np.sin(2 * g)
    eps = np.radians(23.439 - 0.00000036 * n)
    x, y, z = np.cos(lam), np.cos(eps) * np.sin(lam), np.sin(eps) * np.sin(lam)
    th = gmst(t)
    c, s = np.cos(th), np.sin(th)
    return np.stack([c * x + s * y, -s * x + c * y, z], axis=1)


def sun_elevation(t, home):
    sun = sun_ecef(t)
    la, lo = math.radians(home[0]), math.radians(home[1])
    up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    return np.degrees(np.arcsin(np.clip(sun @ up, -1, 1)))


def sunlit(ecef, t):
    """The satellite is in sunlight (not in the Earth's shadow, taken as a cylinder)."""
    sun = sun_ecef(t)
    along = np.einsum("ij,ij->i", ecef, sun)
    perp = np.linalg.norm(ecef - along[:, None] * sun, axis=1)
    return (along > 0) | (perp > EARTH_A)


# ---- passes ----------------------------------------------------------------------------------

@dataclass
class Pass:
    start: float                   # unix time it rises above the horizon (AOS)
    end: float                     # sets (LOS)
    peak: float                    # its highest
    max_el: float
    start_az: float
    peak_az: float
    end_az: float
    visible: bool = False          # lit by the Sun against a dark sky, some of the way: one to watch

    @property
    def duration(self):
        return self.end - self.start


def _cross(sat, home, a, b, rising):
    """The time between a and b (s) when the elevation crosses 0, to a second."""
    for _ in range(12):
        m = (a + b) / 2
        el = track(sat, [m], home).el[0]
        if (el > 0) == rising:
            b = m
        else:
            a = m
        if b - a < 1:
            break
    return (a + b) / 2


def passes(sat, home, start, days=5.0, min_el=0.0, step=30.0):
    """The passes over `home` from `start` (unix) for `days`, whose highest point is at least
    `min_el` degrees. Each found on a coarse grid, its rise and set then found to a second."""
    t = np.arange(start, start + days * 86400, step)
    tr = track(sat, t, home)
    up = (tr.el > 0) & tr.ok
    out = []
    edges = np.flatnonzero(np.diff(up.astype(np.int8)))
    i = 0
    if up[0]:                                         # already up: start at the first sample
        edges = np.concatenate([[-1], edges])
    while i < len(edges):
        a = edges[i]
        b = edges[i + 1] if i + 1 < len(edges) else len(t) - 1
        i += 2
        aos = t[0] if a < 0 else _cross(sat, home, t[a], t[a + 1], True)
        los = t[-1] if b >= len(t) - 1 else _cross(sat, home, t[b], t[b + 1], False)
        fine = np.arange(aos, los + 1, 1.0)
        if len(fine) < 2:
            continue
        ft = track(sat, fine, home)
        k = int(np.argmax(ft.el))
        if ft.el[k] < min_el:
            continue
        dark = sun_elevation(fine[::10], home) < -6
        lit = sunlit(ft.ecef[::10], fine[::10])
        out.append(Pass(float(aos), float(los), float(fine[k]), float(ft.el[k]), float(ft.az[0]),
                        float(ft.az[k]), float(ft.az[-1]), bool(np.any(dark & lit & (ft.el[::10] > 10)))))
    return out


def footprint_km(alt_km, min_el=0.0):
    """How far from the point below it a satellite at alt_km is above min_el degrees: the radius
    of the area that can hear it (great-circle km)."""
    e = math.radians(min_el)
    r = EARTH_A
    lam = math.acos(r / (r + alt_km) * math.cos(e)) - e
    return r * lam
