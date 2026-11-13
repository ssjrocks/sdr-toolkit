"""The logbook: everything decoded, the aircraft and ships seen, their positions over time, and
the files received (pictures decoded, calls recorded).

One SQLite file (``~/.local/share/sdr-toolkit/logbook.sqlite``). Positions are kept
one per craft every 10 s or 300 m, which is enough to replay the traffic and small
(a busy ADS-B day is a few tens of MB); they're dropped after ``KEEP_DAYS``. The
rest stays until it's cleared.
"""

import json
import math
import sqlite3
import time
from pathlib import Path
from . import system

KEEP_DAYS = 30
NEW_SIGHTING = 30 * 60          # seen again after this long counts as another sighting
POS_EVERY = 10.0                # seconds between stored positions of one craft...
POS_MOVED = 300.0               # ...unless it has moved this far (m)


def default_path() -> Path:
    return system.data_dir() / "logbook.sqlite"


def _moved_m(a, b) -> float:
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


class Logbook:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS entries (t REAL, mode TEXT, text TEXT);
            CREATE INDEX IF NOT EXISTS entries_t ON entries (mode, t);
            CREATE TABLE IF NOT EXISTS craft (mode TEXT, key TEXT, name TEXT, first REAL, last REAL,
                                              sightings INTEGER, info TEXT, PRIMARY KEY (mode, key));
            CREATE TABLE IF NOT EXISTS positions (mode TEXT, key TEXT, t REAL, lat REAL, lon REAL,
                                                  alt REAL, heading REAL, label TEXT);
            CREATE INDEX IF NOT EXISTS positions_t ON positions (mode, t);
            CREATE TABLE IF NOT EXISTS coverage (mode TEXT, site TEXT, data TEXT, PRIMARY KEY (mode, site));
            CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY, freq REAL, label TEXT, demod TEXT,
                                             bandwidth INTEGER, decoder TEXT, note TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, t REAL, mode TEXT, kind TEXT, title TEXT,
                                              info TEXT);
            CREATE INDEX IF NOT EXISTS files_t ON files (mode, t);
        """)
        self._last_pos: dict[tuple, tuple] = {}         # (mode, key) -> (t, lat, lon) last stored
        self._craft_seen: dict[tuple, float] = {}        # (mode, key) -> last craft-row update
        self._dirty = False

    # ---- writing --------------------------------------------------------

    def add(self, mode: str, text: str, t: float | None = None):
        self.db.execute("INSERT INTO entries VALUES (?, ?, ?)", (t or time.time(), mode, text))
        self._dirty = True

    def add_file(self, mode: str, kind: str, path, title: str, info: dict | None = None, t: float | None = None):
        """A file received: kind "picture" (decoded) or "call" (recorded). Listed once per path."""
        self.db.execute("INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?, ?, ?)",
                        (str(path), t or time.time(), mode, kind, title, json.dumps(info or {})))
        self._dirty = True

    def remove_file(self, path):
        self.db.execute("DELETE FROM files WHERE path=?", (str(path),))
        self._dirty = True

    def see(self, mode: str, key: str, name="", info: dict | None = None, lat=None, lon=None, alt=None,
            heading=None, label="", t: float | None = None):
        """An aircraft or ship heard: updates its row, and stores the position if it's due."""
        t = t or time.time()
        k = (mode, key)
        if t - self._craft_seen.get(k, 0) >= 5 or k not in self._craft_seen:
            self._craft_seen[k] = t
            row = self.db.execute("SELECT name, last, sightings, info FROM craft WHERE mode=? AND key=?",
                                  k).fetchone()
            if row is None:
                self.db.execute("INSERT INTO craft VALUES (?, ?, ?, ?, ?, 1, ?)",
                                (mode, key, name, t, t, json.dumps(info or {})))
            else:
                old_name, last, sightings, old_info = row
                merged = json.loads(old_info or "{}")
                merged.update({a: b for a, b in (info or {}).items() if b not in ("", None)})
                self.db.execute("UPDATE craft SET name=?, last=?, sightings=?, info=? WHERE mode=? AND key=?",
                                (name or old_name, t, sightings + (t - last > NEW_SIGHTING), json.dumps(merged),
                                 mode, key))
            self._dirty = True
        if lat is None or lon is None:
            return
        prev = self._last_pos.get(k)
        if prev and t - prev[0] < POS_EVERY and _moved_m(prev[1:], (lat, lon)) < POS_MOVED:
            return
        self._last_pos[k] = (t, lat, lon)
        self.db.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (mode, key, t, lat, lon, alt, heading, label))
        self._dirty = True

    def flush(self):
        if self._dirty:
            self.db.commit()
            self._dirty = False

    def prune(self, days=KEEP_DAYS):
        self.db.execute("DELETE FROM positions WHERE t < ?", (time.time() - days * 86400,))
        self.db.commit()

    def clear(self, mode: str | None = None):
        for table in ("entries", "craft", "positions", "coverage", "files"):     # (the files themselves stay)
            if mode:
                self.db.execute(f"DELETE FROM {table} WHERE mode=?", (mode,))
            else:
                self.db.execute(f"DELETE FROM {table}")
        self.db.commit()
        self._last_pos.clear()
        self._craft_seen.clear()

    # ---- reading --------------------------------------------------------

    def entries(self, mode=None, search="", limit=5000) -> list[tuple]:
        """(time, mode, text), newest first."""
        q, args = "SELECT t, mode, text FROM entries WHERE 1", []
        if mode:
            q += " AND mode=?"
            args.append(mode)
        if search:
            q += " AND text LIKE ?"
            args.append(f"%{search}%")
        return self.db.execute(q + " ORDER BY t DESC LIMIT ?", args + [limit]).fetchall()

    def files(self, mode=None, search="", kind=None, limit=20000) -> list[dict]:
        """The files received, newest first: {path, t, mode, kind, title, info}."""
        q, args = "SELECT path, t, mode, kind, title, info FROM files WHERE 1", []
        if mode:
            q += " AND mode=?"
            args.append(mode)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        if search:
            q += " AND (title LIKE ? OR info LIKE ? OR path LIKE ?)"
            args += [f"%{search}%"] * 3
        rows = self.db.execute(q + " ORDER BY t DESC LIMIT ?", args + [limit]).fetchall()
        return [{"path": p, "t": t, "mode": m, "kind": k, "title": ti, "info": json.loads(i or "{}")}
                for p, t, m, k, ti, i in rows]

    def craft(self, mode=None, search="", limit=5000) -> list[dict]:
        q, args = "SELECT mode, key, name, first, last, sightings, info FROM craft WHERE 1", []
        if mode:
            q += " AND mode=?"
            args.append(mode)
        if search:
            q += " AND (key LIKE ? OR name LIKE ? OR info LIKE ?)"
            args += [f"%{search}%"] * 3
        rows = self.db.execute(q + " ORDER BY last DESC LIMIT ?", args + [limit]).fetchall()
        return [{"mode": m, "key": k, "name": n, "first": f, "last": la, "sightings": s,
                 "info": json.loads(i or "{}")} for m, k, n, f, la, s, i in rows]

    def craft_info(self, mode, key) -> dict | None:
        row = self.db.execute("SELECT name, first, last, sightings, info FROM craft WHERE mode=? AND key=?",
                              (mode, key)).fetchone()
        if row is None:
            return None
        n, f, la, s, i = row
        return {"name": n, "first": f, "last": la, "sightings": s, "info": json.loads(i or "{}")}

    def positions(self, mode, t0, t1) -> list[tuple]:
        """(key, t, lat, lon, alt, heading, label) between two times, oldest first."""
        return self.db.execute("SELECT key, t, lat, lon, alt, heading, label FROM positions "
                               "WHERE mode=? AND t BETWEEN ? AND ? ORDER BY t", (mode, t0, t1)).fetchall()

    def time_range(self, mode) -> tuple | None:
        row = self.db.execute("SELECT MIN(t), MAX(t) FROM positions WHERE mode=?", (mode,)).fetchone()
        return row if row and row[0] is not None else None

    def counts(self, mode=None) -> dict:
        where, args = (" WHERE mode=?", (mode,)) if mode else ("", ())
        return {table: self.db.execute(f"SELECT COUNT(*) FROM {table}{where}", args).fetchone()[0]
                for table in ("entries", "craft", "positions", "files")}

    # ---- coverage (see coverage.py) -------------------------------------

    def load_coverage(self, mode, site) -> dict | None:
        row = self.db.execute("SELECT data FROM coverage WHERE mode=? AND site=?", (mode, site)).fetchone()
        return json.loads(row[0]) if row else None

    def save_coverage(self, mode, site, data: dict):
        self.db.execute("INSERT OR REPLACE INTO coverage VALUES (?, ?, ?)", (mode, site, json.dumps(data)))
        self._dirty = True

    # ---- frequency tags (Spectrum & receiver) ---------------------------

    def add_tag(self, freq, label, demod="", bandwidth=0, decoder="", note="") -> int:
        cur = self.db.execute("INSERT INTO tags (freq, label, demod, bandwidth, decoder, note, created) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)", (float(freq), label, demod, int(bandwidth or 0),
                                                               decoder, note, time.time()))
        self.db.commit()
        return cur.lastrowid

    def tags(self, lo=0.0, hi=1e12) -> list[dict]:
        rows = self.db.execute("SELECT id, freq, label, demod, bandwidth, decoder, note, created FROM tags "
                               "WHERE freq BETWEEN ? AND ? ORDER BY freq", (lo, hi)).fetchall()
        return [dict(zip(("id", "freq", "label", "demod", "bandwidth", "decoder", "note", "created"), r)) for r in rows]

    def delete_tag(self, tag_id):
        self.db.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        self.db.commit()

    def close(self):
        self.flush()
        self.db.close()
