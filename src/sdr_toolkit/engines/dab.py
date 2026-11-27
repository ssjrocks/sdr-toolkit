"""DAB / DAB+ digital radio through welle-cli (welle.io): the ensembles on band III, their stations, what's
playing (DLS), each station's slideshow picture, and its audio on request.

    python -m sdr_toolkit.engines.dab --channels 9A,9B,9C [--dwell S] --input rtl_sdr | rtl_tcp,HOST:PORT | FILE
        [--gain DB] --slides DIR

welle-cli runs on one channel at a time with its web server on a free port, decoding every station; its
/mux.json is read every 2 s and each station written as a JSON line, its slide saved in --slides when it changes.
With several channels it moves on after --dwell s (sooner when nothing is there), round and round; with one it
stays. A recording (unsigned 8-bit IQ at 2.048 MS/s) is one channel.

Writes JSON lines:
  {"event": "channel", "channel": "9A", "port": P}            welle-cli is up on it (audio: /mp3/<sid>)
  {"event": "ensemble", "channel", "label", "id", "snr", "utc"}
  {"event": "service", "channel", "ensemble", "sid", "label", "short", "dls", "mode", "pty", "slide", ...}
  {"event": "empty", "channel": "9A"}                         nothing decodable there
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Band III blocks (MHz), as ETSI EN 300 401 lists them.
BLOCKS = {
    "5A": 174.928, "5B": 176.640, "5C": 178.352, "5D": 180.064, "6A": 181.936, "6B": 183.648, "6C": 185.360,
    "6D": 187.072, "7A": 188.928, "7B": 190.640, "7C": 192.352, "7D": 194.064, "8A": 195.936, "8B": 197.648,
    "8C": 199.360, "8D": 201.072, "9A": 202.928, "9B": 204.640, "9C": 206.352, "9D": 208.064, "10A": 209.936,
    "10N": 210.096, "10B": 211.648, "10C": 213.360, "10D": 215.072, "11A": 216.928, "11N": 217.088,
    "11B": 218.640, "11C": 220.352, "11D": 222.064, "12A": 223.936, "12N": 224.096, "12B": 225.648,
    "12C": 227.360, "12D": 229.072, "13A": 230.784, "13B": 232.496, "13C": 234.208, "13D": 235.776,
    "13E": 237.488, "13F": 239.200,
}
POLL = 2.0
EMPTY_AFTER = 8.0                  # s without an ensemble: nothing on this channel


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def label(x) -> str:
    if isinstance(x, dict):
        return str(x.get("label", "")).strip()
    return str(x or "").strip()


def get(port, path, timeout=2.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.read()


def services(mux: dict, channel: str, slides: Path, port: int, seen: dict) -> list[dict]:
    out = []
    ens = label(mux.get("ensemble", {}).get("label"))
    for s in mux.get("services", []):
        sid = str(s.get("sid", ""))
        name = label(s.get("label"))
        if not sid or not name:
            continue
        sub = next((c.get("subchannel") or {} for c in s.get("components", []) if c.get("primary")), {})
        d = {"event": "service", "channel": channel, "ensemble": ens, "sid": sid, "label": name,
             "short": str((s.get("label") or {}).get("shortlabel", "")).strip(),
             "dls": str((s.get("dls") or {}).get("label", "")).strip(), "mode": s.get("mode") or "",
             "pty": s.get("ptystring") or "", "bitrate": sub.get("bitrate"), "language": s.get("languagestring") or "",
             "audiolevel": s.get("audiolevel"), "errors": s.get("errorcounters")}
        mot = s.get("mot") or {}
        change = mot.get("lastchange")
        path = slides / f"{channel}_{sid}.img"
        if change and seen.get(sid) != change:
            try:
                img = get(port, f"/slide/{sid}")
                if img:
                    path.write_bytes(img)
                    seen[sid] = change
            except OSError:
                pass
        if path.is_file():
            d["slide"] = str(path)
        out.append(d)
    return out


def run_channel(ch, a, slides, deadline, seen):
    """welle-cli on one channel until *deadline* (None: for ever); False if nothing was there."""
    port = free_port()
    work = tempfile.mkdtemp(prefix="sdr-toolkit-dab-")
    cmd = ["welle-cli", "-w", str(port), "-D", "-T"]
    if os.path.isfile(a.input):
        cmd += ["-f", os.path.abspath(a.input)]             # welle-cli runs in a folder of its own
    else:
        cmd += ["-c", ch, "-F", a.input]
        if a.gain:
            cmd += ["-g", a.gain]
    p = subprocess.Popen(cmd, cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    emit({"event": "channel", "channel": ch, "port": port, "mhz": BLOCKS.get(ch)})
    started = time.monotonic()
    got = False
    last_json = ""
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(POLL)
            if p.poll() is not None:
                err = (p.stderr.read() or "").strip().splitlines()
                tail = next((x for x in reversed(err) if x.strip()), "")
                if not got:
                    emit({"event": "empty", "channel": ch, "why": tail[:200]})
                return got
            try:
                mux = json.loads(get(port, "/mux.json"))
            except (OSError, ValueError):
                continue
            ens = mux.get("ensemble") or {}
            if label(ens.get("label")):
                if not got:
                    got = True
                demod = mux.get("demodulator") or {}
                d = {"event": "ensemble", "channel": ch, "label": label(ens.get("label")), "id": ens.get("id"),
                     "snr": demod.get("snr"), "fic_errors": (demod.get("fic") or {}).get("numcrcerrors"),
                     "utc": mux.get("utctime")}
                text = json.dumps(d, sort_keys=True)
                if text != last_json:
                    emit(d)
                    last_json = text
                for s in services(mux, ch, slides, port, seen):
                    emit(s)
            elif not got and time.monotonic() - started > EMPTY_AFTER and deadline is not None:
                emit({"event": "empty", "channel": ch})
                return False
        return got
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default="9A,9B,9C")
    ap.add_argument("--dwell", type=float, default=30.0)
    ap.add_argument("--input", default="rtl_sdr")
    ap.add_argument("--gain", default="")
    ap.add_argument("--slides", required=True)
    a = ap.parse_args(argv)
    if shutil.which("welle-cli") is None:
        sys.exit("dab: welle-cli isn't installed (the welle.io package)")
    slides = Path(a.slides).resolve()
    slides.mkdir(parents=True, exist_ok=True)
    chans = [c.strip().upper() for c in a.channels.split(",") if c.strip()]
    bad = [c for c in chans if c not in BLOCKS]
    if bad:
        sys.exit(f"dab: not band III channels: {', '.join(bad)}")
    seen: dict = {}
    if os.path.isfile(a.input) or len(chans) == 1:
        run_channel(chans[0] if chans else "file", a, slides, None if not os.path.isfile(a.input) else
                    time.monotonic() + a.dwell, seen)
        return
    while True:
        for ch in chans:
            run_channel(ch, a, slides, time.monotonic() + a.dwell, seen)


if __name__ == "__main__":
    main()
