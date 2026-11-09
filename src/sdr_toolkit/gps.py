"""Position from a GPS receiver: NMEA over a USB serial port, or gpsd.

The serial port is only read, never written. On other USB-serial devices (a
radio's programming cable, a microcontroller) the DTR and RTS lines can key a
transmitter or reset the board, and Linux raises them briefly whenever a port
is opened; pyserial lowers them straight after. So "Automatic" only opens gpsd
or a device whose name says it's a GPS, and anything else must be chosen.
"""

import json
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

BY_ID = Path("/dev/serial/by-id")
LIKELY = re.compile(r"gps|gnss|u-?blox|sirf|nmea|garmin|globalsat|bu-?353|vk-?16|g-?mouse|quectel|mediatek", re.I)
BAUDS = (9600, 4800, 38400, 115200, 57600, 19200)


@dataclass
class Fix:
    lat: float
    lon: float
    alt: float | None = None
    sats: int | None = None
    mode: str = "fix"          # "2D", "3D" or "fix"
    when: float = 0.0          # time.time() it was received


def devices() -> list[tuple[str, str, bool]]:
    """(path, name, looks like a GPS) for each USB serial device."""
    out = []
    if BY_ID.is_dir():
        for p in sorted(BY_ID.iterdir()):
            name = p.name.removeprefix("usb-").rsplit("-if", 1)[0].replace("_", " ")
            out.append((str(p), name, bool(LIKELY.search(p.name))))
    return out


def gpsd_running(host="127.0.0.1", port=2947) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def auto_source() -> str | None:
    if gpsd_running():
        return "gpsd"
    for path, _, likely in devices():
        if likely:
            return path
    return None


def nmea_ok(line: str) -> bool:
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return False
    body, cs = line[1:].split("*", 1)
    x = 0
    for c in body:
        x ^= ord(c)
    return cs[:2].upper() == f"{x:02X}"


def _coord(value, hemi, degree_digits):
    if not value:
        return None
    deg = float(value[:degree_digits])
    minutes = float(value[degree_digits:])
    v = deg + minutes / 60
    return -v if hemi in ("S", "W") else v


class NMEA:
    """Keeps the latest fix from GGA / RMC / GSA sentences (any talker: GP, GN, GL...)."""

    def __init__(self):
        self.fix: Fix | None = None
        self._mode = "fix"
        self._sats = None

    def feed(self, line: str) -> Fix | None:
        if not nmea_ok(line):
            return None
        f = line.strip()[1:].split("*")[0].split(",")
        kind = f[0][2:]
        try:
            if kind == "GGA" and len(f) > 9:
                if f[6] in ("", "0"):
                    return None                       # no fix
                lat, lon = _coord(f[2], f[3], 2), _coord(f[4], f[5], 3)
                self._sats = int(f[7]) if f[7] else None
                alt = float(f[9]) if f[9] else None
                if lat is not None and lon is not None:
                    self.fix = Fix(lat, lon, alt, self._sats, self._mode, time.time())
                    return self.fix
            elif kind == "RMC" and len(f) > 6:
                if f[2] != "A":
                    return None
                lat, lon = _coord(f[3], f[4], 2), _coord(f[5], f[6], 3)
                if lat is not None and lon is not None:
                    alt = self.fix.alt if self.fix else None
                    self.fix = Fix(lat, lon, alt, self._sats, self._mode, time.time())
                    return self.fix
            elif kind == "GSA" and len(f) > 2:
                self._mode = {"2": "2D", "3": "3D"}.get(f[2], self._mode)
        except ValueError:
            return None
        return None


class GpsReader(threading.Thread):
    """Reads a source in the background; ``fix`` and ``state`` say how it's going."""

    def __init__(self, source: str, baud="auto"):
        super().__init__(daemon=True)
        self.source = source
        self.baud = baud
        self.fix: Fix | None = None
        self.state = "starting"
        self._halt = threading.Event()

    def stop(self):
        self._halt.set()

    def run(self):
        while not self._halt.is_set():
            try:
                if self.source == "gpsd":
                    self._gpsd()
                else:
                    self._serial()
            except Exception as e:  # shown in the GPS window; try again shortly
                self.state = f"error: {e}"
                log.info("GPS %s: %s", self.source, e)
            self._halt.wait(5)

    # ---- serial NMEA --------------------------------------------------

    def _open(self, baud):
        import serial
        s = serial.Serial()
        s.port = self.source
        s.baudrate = baud
        s.timeout = 1
        s.dtr = False            # lowered as soon as it's open
        s.rts = False
        s.open()
        return s

    def _find_baud(self):
        for baud in BAUDS:
            if self._halt.is_set():
                return None
            self.state = f"looking for NMEA at {baud} baud"
            with self._open(baud) as s:
                end = time.time() + 2.5
                while time.time() < end:
                    if nmea_ok(s.readline().decode("ascii", "replace")):
                        return baud
        return None

    def _serial(self):
        baud = int(self.baud) if str(self.baud).isdigit() else self._find_baud()
        if baud is None:
            self.state = "no NMEA sentences at any speed: is this the GPS?"
            return
        parser = NMEA()
        with self._open(baud) as s:
            self.state = f"reading at {baud} baud, waiting for a fix"
            while not self._halt.is_set():
                line = s.readline().decode("ascii", "replace")
                if not line:
                    continue
                fix = parser.feed(line)
                if fix:
                    self.fix = fix
                    self.state = "fix"

    # ---- gpsd ---------------------------------------------------------

    def _gpsd(self):
        with socket.create_connection(("127.0.0.1", 2947), timeout=5) as sock:
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            sock.settimeout(2)
            self.state = "connected to gpsd, waiting for a fix"
            buf = b""
            sats = None
            while not self._halt.is_set():
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    raise OSError("gpsd closed the connection")
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("class") == "SKY":
                        used = [s for s in msg.get("satellites", []) if s.get("used")]
                        sats = len(used) if used else msg.get("uSat", sats)
                    elif msg.get("class") == "TPV" and msg.get("mode", 0) >= 2 and "lat" in msg:
                        self.fix = Fix(msg["lat"], msg["lon"], msg.get("altMSL", msg.get("alt")), sats,
                                       "3D" if msg["mode"] == 3 else "2D", time.time())
                        self.state = "fix"
