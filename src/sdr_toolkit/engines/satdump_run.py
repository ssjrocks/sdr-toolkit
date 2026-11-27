"""SatDump for the toolkit: weather-satellite pictures (METEOR-M LRPT) pass by pass, or Inmarsat STD-C / AERO
messages from a geostationary satellite, without SatDump's own window.

    python -m sdr_toolkit.engines.satdump_run --pipeline ID --frequency HZ --samplerate SPS --out DIR
        --source rtltcp:HOST:PORT | rtlsdr | FILE [--gain DB]
        [--norad 57166,59051 --lat LAT --lon LON --min-el DEG]          polar: one recording per pass

With --norad it waits for the next pass of those satellites that rises above --min-el (from the satellite
tracker's elements), runs `satdump live` for the pass into its own folder, and lists the pictures SatDump made.
Without, it runs `satdump live` for as long as it's left on, and lists the messages it saves. A recording (cu8)
is processed with `satdump PIPELINE baseband`.

Writes JSON lines:
  {"event": "next", "name", "norad", "aos", "los", "max_el"}        the pass it waits for (unix times)
  {"event": "start", "name", "dir"}                                  recording a pass, or listening
  {"event": "progress", "snr", "peak_snr", "ber", "lock"}            SatDump's, every ~10 s
  {"event": "pass", "name", "dir", "images": [...], "peak_snr", "locked", "max_el"}
  {"event": "message", "path", "text"}                               a message SatDump saved (STD-C, AERO)
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PROGRESS = re.compile(r"SNR : ([-\d.naif]+)dB, Peak SNR: ([-\d.naif]+)dB")
BER = re.compile(r"Viterbi BER : ([\d.]+)%, Lock : (\w+)")
IMAGE_EXT = (".png", ".jpg", ".jpeg")
MESSAGE_EXT = (".txt", ".json", ".msg")
SATDUMP_FILES = {"dataset.json", "telemetry.json", "product.json"}   # its own, not messages
SEGMENT = 600                       # s: a geostationary recording's parts


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def _num(s):
    try:
        v = float(s)
        return None if v != v or v in (float("inf"), float("-inf")) else round(v, 2)
    except ValueError:
        return None


def source_args(src: str, gain: str) -> list[str]:
    if src.startswith("rtltcp:"):
        _, host, port = src.split(":")
        out = ["--source", "rtltcp", "--ip_address", host, "--port", port]
    else:
        out = ["--source", src]
    if gain:
        out += ["--gain", gain]
    return out


_child = None


def _stop(*_):
    """The page stopped us: SatDump is killed (SIGKILL: what it does on SIGINT or SIGTERM hasn't been seen, and
    an abort would be a crash report on the desktop). What it saved so far stays."""
    if _child is not None and _child.poll() is None:
        _child.kill()
    sys.exit(0)


def run_satdump(cmd, seconds, state, folder: Path | None = None):
    """Run SatDump (ended by its own --timeout), passing its progress on; for messages, also what appears in
    *folder*."""
    global _child
    p = _child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    seen = state.setdefault("seen", set())
    try:
        for line in p.stdout:
            line = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip()
            m = PROGRESS.search(line)
            if m:
                state["snr"], peak = _num(m.group(1)), _num(m.group(2))
                if peak is not None:
                    state["peak_snr"] = max(state.get("peak_snr") or 0.0, peak)
            m = BER.search(line)
            if m:
                state["ber"], state["lock"] = float(m.group(1)), m.group(2)
                if m.group(2) == "SYNCED":
                    state["locked"] = True
                emit({"event": "progress", "snr": state.get("snr"), "peak_snr": state.get("peak_snr"),
                      "ber": state["ber"], "lock": state["lock"]})
            if "(E)" in line and "Progress" not in line:
                emit({"event": "log", "text": line.split("(E)", 1)[1].strip()})
            if folder is not None and folder.is_dir():
                for f in sorted(folder.rglob("*")):
                    if (f.suffix.lower() in MESSAGE_EXT and f.name not in SATDUMP_FILES and f not in seen
                            and f.is_file()):
                        seen.add(f)
                        try:
                            text = f.read_text(errors="replace")[:600]
                        except OSError:
                            text = ""
                        emit({"event": "message", "path": str(f), "text": text})
        p.wait(30 + (seconds or 0))
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    finally:
        _child = None


def images_in(folder: Path) -> list[str]:
    return [str(f) for f in sorted(folder.rglob("*")) if f.suffix.lower() in IMAGE_EXT and f.is_file()]


def next_pass(norads, lat, lon, min_el, after):
    from ..satellites import data, orbit
    sats = data.load(offline=True) or data.load()
    best = None
    for n in norads:
        s = sats.get(n)
        if s is None:
            continue
        rec = orbit.satrec(s.line1, s.line2)
        for p in orbit.passes(rec, (lat, lon), after, days=2.0, min_el=min_el):
            if p.end > after and (best is None or p.start < best[1].start):
                best = (s, p)
            break
    return best


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline", required=True)
    ap.add_argument("--frequency", type=float, required=True)
    ap.add_argument("--samplerate", type=float, default=1.024e6)
    ap.add_argument("--source", default="rtlsdr")
    ap.add_argument("--gain", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--norad", default="")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--min-el", type=float, default=15.0)
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if os.path.isfile(a.source):                          # a recording
        folder = out / time.strftime("%Y-%m-%d_%H-%M-%S_recording")
        emit({"event": "start", "name": "recording", "dir": str(folder)})
        state = {}
        run_satdump(["satdump", a.pipeline, "baseband", os.path.abspath(a.source), str(folder), "--samplerate",
                     str(a.samplerate), "--baseband_format", "u8"], 3600, state,
                    folder if a.pipeline.startswith("inmarsat") else None)
        emit({"event": "pass", "name": "recording", "dir": str(folder), "images": images_in(folder),
              "peak_snr": state.get("peak_snr"), "locked": bool(state.get("locked"))})
        return
    live = ["satdump", "live", a.pipeline]
    rest = ["--samplerate", str(a.samplerate), "--frequency", str(a.frequency), *source_args(a.source, a.gain)]
    norads = [int(x) for x in a.norad.split(",") if x.strip()]
    if not norads:                                         # geostationary: listen for as long as it's left on,
        folder = out / time.strftime("%Y-%m-%d_%H-%M-%S")   # in segments SatDump ends by itself
        emit({"event": "start", "name": a.pipeline, "dir": str(folder)})
        state = {}
        n = 0
        while True:
            n += 1
            run_satdump(live + [str(folder / f"part{n:03d}")] + rest + ["--timeout", str(SEGMENT)], SEGMENT, state,
                        folder)
    if a.lat is None or a.lon is None:
        sys.exit("satdump_run: your location is needed to know when the satellites pass (Settings → Your location)")
    while True:
        found = next_pass(norads, a.lat, a.lon, a.min_el, time.time())
        if found is None:
            sys.exit("satdump_run: no pass found in the next two days (the satellites' elements may be missing: "
                     "open the satellite tracker once to fetch them)")
        sat, p = found
        emit({"event": "next", "name": sat.name, "norad": sat.norad, "aos": p.start, "los": p.end,
              "max_el": round(p.max_el, 1)})
        while time.time() < p.start:
            time.sleep(min(5.0, max(0.1, p.start - time.time())))
        folder = out / (time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(p.start)) + "_" + sat.name.replace(" ", "_"))
        emit({"event": "start", "name": sat.name, "dir": str(folder)})
        state = {}
        secs = max(30, int(p.end - time.time()))
        run_satdump(live + [str(folder)] + rest + ["--timeout", str(secs)], secs, state)
        emit({"event": "pass", "name": sat.name, "dir": str(folder), "images": images_in(folder),
              "peak_snr": state.get("peak_snr"), "locked": bool(state.get("locked")), "max_el": round(p.max_el, 1)})
        time.sleep(5)


if __name__ == "__main__":
    main()
