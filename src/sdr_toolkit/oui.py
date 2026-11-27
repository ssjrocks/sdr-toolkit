"""Who made a device, from the first three bytes of its MAC address or EUI-64: the IEEE's register as Linux
distributions ship it (the ieee-data package's oui.csv), when it's there."""

import csv
from functools import lru_cache
from pathlib import Path

PATHS = (Path("/usr/share/ieee-data/oui.csv"), Path("/usr/share/misc/oui.csv"))


@lru_cache(maxsize=1)
def _table() -> dict[str, str]:
    for p in PATHS:
        if p.is_file():
            out = {}
            try:
                with open(p, newline="", encoding="utf-8", errors="replace") as f:
                    for row in csv.reader(f):
                        if len(row) >= 3 and len(row[1]) == 6:
                            out[row[1].upper()] = row[2].strip()
            except OSError:
                continue
            return out
    return {}


def vendor(address: str) -> str:
    """The maker for an address ("00:17:88:..." or "00-17-88-..."), or "". A locally administered (random)
    address has none."""
    hexs = "".join(c for c in str(address) if c.isalnum()).upper()
    if len(hexs) < 6:
        return ""
    if int(hexs[0:2], 16) & 0x02:                     # locally administered: random, no maker
        return ""
    return _table().get(hexs[:6], "")
