"""The aircraft database readsb uses for registrations, types and operators.

It's tar1090-db (github.com/wiedehopf/tar1090-db, built from public registers),
downloaded by the app into ``~/.local/share/sdr-toolkit/aircraft.csv.gz`` and
refreshed when it's a month old.
"""

import logging
import time
import urllib.request
from pathlib import Path

from . import __version__
from . import system

log = logging.getLogger(__name__)
URL = "https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
USER_AGENT = f"SDR-Toolkit/{__version__} (+https://github.com/ssjrocks/sdr-toolkit)"
MAX_AGE = 30 * 86400


def path() -> Path:
    return system.data_dir() / "aircraft.csv.gz"


def present() -> bool:
    return path().is_file() and path().stat().st_size > 1_000_000


def due() -> bool:
    return not present() or time.time() - path().stat().st_mtime > MAX_AGE


def download(url=URL):
    """Download to a temporary name, check it's gzip, then replace the old one. Raises on failure."""
    dest = path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
    with open(tmp, "rb") as f:
        if f.read(2) != b"\x1f\x8b" or tmp.stat().st_size < 1_000_000:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("the aircraft database download wasn't a database")
    tmp.replace(dest)
    log.info("aircraft database updated (%d bytes)", dest.stat().st_size)
