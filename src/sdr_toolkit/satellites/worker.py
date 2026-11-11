"""Satellites' next passes, worked out in a process of its own so the window's Python isn't kept
busy (python -m sdr_toolkit.satellites.worker).

Reads one JSON job on stdin: {"home": [lat, lon], "start", "days", "min_el", "step",
"sats": [[norad, line1, line2], ...]}; writes a JSON line per satellite as it's done:
[norad, [[start, end, peak, max_el, start_az, peak_az, end_az, visible], ...]].
"""

import json
import sys

from . import orbit

FIELDS = ("start", "end", "peak", "max_el", "start_az", "peak_az", "end_az", "visible")


def main():
    job = json.load(sys.stdin)
    home = tuple(job["home"])
    for norad, line1, line2 in job["sats"]:
        try:
            ps = orbit.passes(orbit.satrec(line1, line2), home, job["start"], job["days"], job["min_el"],
                              step=job.get("step", 60.0))
        except Exception:  # elements SGP4 can't use (decayed): no passes
            ps = []
        sys.stdout.write(json.dumps([norad, [[getattr(p, f) for f in FIELDS] for p in ps]]) + "\n")
        sys.stdout.flush()


def to_passes(rows):
    return [orbit.Pass(*row) for row in rows]


if __name__ == "__main__":
    main()
