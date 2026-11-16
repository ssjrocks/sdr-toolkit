"""Using an RTL-SDR or HackRF on another PC through SDR Tools Relay (github.com/ssjrocks/sdr-tools-relay).

The relay protocol (version 1, the relay's sdr_relay/protocol.py has the full description):
the relay sends a JSON hello with a nonce, the client answers with HMAC-SHA256(password,
nonce), the relay says {"ok": true} and from then on speaks plain rtl_tcp.

While a relay is connected the window runs a ``Bridge``: a local rtl_tcp server on
127.0.0.1 that does the handshake for each decoder that connects and then passes the
bytes through. rtl_433, DSD-FME and GNSS-SDR connect to it as to any rtl_tcp server;
the others read relay_stream's output (python -m sdr_toolkit.relay_stream).

A relay's HackRF streams as an RTL-SDR does, at 2-20 MS/s. A decoder that asks for less (rtl_433's
250 kS/s) gets it all the same: the bridge asks the HackRF for a whole multiple and filters and
decimates it down (HackRFLink). It also shows the decoders an R820T in rtl_tcp's header (their gain
tables know that one), and sends the HackRF's own settings (RF amp, antenna power, baseband filter)
at the start of each session.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

import numpy as np

from . import __version__

log = logging.getLogger(__name__)
PORT = 47320
DISCOVERY_PORT = 47321
DISCOVER = b"SDR-TOOLS-RELAY? 1"
DIRECTORY = "https://ssjrocks.com/api/sdr-relay/v1/relays"
USER_AGENT = f"SDR-Toolkit/{__version__} (+https://github.com/ssjrocks/sdr-toolkit)"
RELAY_RELEASES = "https://api.github.com/repos/ssjrocks/sdr-tools-relay/releases/latest"
BUSY_RETRY = 6.0


class RelayError(RuntimeError):
    pass


def auth(password: str, nonce: str) -> str:
    return hmac.new(password.encode(), nonce.encode(), hashlib.sha256).hexdigest() if password else ""


def _read_line(sock, limit=8192) -> tuple[dict, bytes]:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(2048)
        if not chunk:
            raise RelayError("the relay closed the connection")
        buf += chunk
        if len(buf) > limit:
            raise RelayError("that isn't an SDR Tools Relay")
    head, _, rest = buf.partition(b"\n")
    try:
        return json.loads(head), rest
    except ValueError:
        raise RelayError("that isn't an SDR Tools Relay") from None


class Busy(RelayError):
    pass


# rtl_tcp and the relay's HackRF commands (the relay's sdr_relay/protocol.py)
SET_FREQ, SET_RATE = 1, 2
SET_FREQ_HI, SET_LNA_GAIN, SET_VGA_GAIN, SET_AMP, SET_ANTENNA_POWER, SET_BASEBAND_FILTER, SET_CLKOUT, \
    SET_TXVGA_GAIN = range(0x40, 0x48)
HACKRF_MIN_RATE, HACKRF_MAX_RATE = 2_000_000, 20_000_000
R820T = 5


def command(cmd, value) -> bytes:
    return struct.pack(">BI", cmd, int(value) & 0xFFFFFFFF)


def hackrf_rate(wanted: int) -> tuple[int, int]:
    """(the HackRF's rate, the decimation) for a decoder that wants `wanted` samples a second."""
    if wanted >= HACKRF_MIN_RATE:
        return min(int(wanted), HACKRF_MAX_RATE), 1
    k = -(-HACKRF_MIN_RATE // int(wanted))                   # the smallest whole multiple of 2 MS/s or more
    return int(wanted) * k, k


class Decimator:
    """cu8 IQ in, cu8 IQ at 1/k the rate out: a low-pass FIR (to 0.8 of the new Nyquist) and every k-th
    sample, keeping its state from one block to the next."""

    def __init__(self, k: int):
        from scipy.signal import firwin
        self.k = k
        self.taps = firwin(16 * k + 1, 0.8 / k).astype(np.float32)
        self.zi = np.zeros(len(self.taps) - 1, np.complex64)
        self.phase = 0
        self.odd = b""

    def __call__(self, data: bytes) -> bytes:
        from scipy.signal import lfilter
        data = self.odd + data
        n = len(data) // 2 * 2
        self.odd = data[n:]
        if not n:
            return b""
        u = np.frombuffer(data[:n], np.uint8).astype(np.float32) - 127.5
        x = u[0::2] + 1j * u[1::2]
        y, self.zi = lfilter(self.taps, 1.0, x, zi=self.zi)
        out = y[self.phase::self.k]
        self.phase = (self.phase - len(x)) % self.k
        b = np.empty(2 * len(out), np.uint8)
        b[0::2] = np.clip(np.round(out.real + 127.5), 0, 255)
        b[1::2] = np.clip(np.round(out.imag + 127.5), 0, 255)
        return b.tobytes()


def hackrf_commands(settings: dict) -> bytes:
    """The relay commands for a HackRF's settings (those set: RF amp, antenna power, baseband filter, LNA and
    VGA gain)."""
    return b"".join(command(c, int(settings[k])) for k, c in (
        ("amp", SET_AMP), ("antenna", SET_ANTENNA_POWER), ("filter", SET_BASEBAND_FILTER),
        ("lna", SET_LNA_GAIN), ("vga", SET_VGA_GAIN)) if settings.get(k) is not None)


class HackRFLink:
    """One decoder's session with a relay's HackRF: its commands on the way out (a sample rate it can't
    have is asked for as a multiple), and the samples on the way back (decimated to the rate it asked for)."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})       # lna and vga set by hand replace the decoders' own gain
        self.decimate = None
        self._cmd = b""
        self._head = b""
        self._lock = threading.Lock()

    def outgoing(self, data: bytes) -> bytes:
        data = self._cmd + data
        n = len(data) // 5 * 5
        self._cmd = data[n:]
        out = b""
        for i in range(0, n, 5):
            cmd, value = struct.unpack(">BI", data[i:i + 5])
            if cmd == SET_RATE:
                rate, k = hackrf_rate(value)
                with self._lock:
                    self.decimate = Decimator(k) if k > 1 else None
                out += command(SET_RATE, rate)
            elif cmd in (3, 4, 13) and self.settings.get("lna") is not None and self.settings.get("vga") is not None:
                # gain mode / gain / gain step: the LNA and VGA set by hand win
                out += command(SET_LNA_GAIN, self.settings["lna"]) + command(SET_VGA_GAIN, self.settings["vga"])
            else:
                out += data[i:i + 5]
        return out

    def incoming(self, data: bytes) -> bytes:
        if len(self._head) < 12:                        # rtl_tcp's header: an R820T, for the decoders' gain tables
            need = 12 - len(self._head)
            self._head += data[:need]
            data = data[need:]
            if len(self._head) < 12:
                return b""
            head = self._head[:4] + struct.pack(">I", R820T) + self._head[8:12]
            return head + self.incoming(data) if data else head
        with self._lock:
            d = self.decimate
        return d(data) if d is not None else data


def open_session(host, port, password="", timeout=8.0, check=False):
    """Connect and log in. Returns (socket, hello, answer, bytes already received after it).
    With *check*, only the password is checked: the relay doesn't open its dongle (relays before
    0.1.2 ignore it and open it)."""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError as e:
        raise RelayError(f"Couldn't reach {host}:{port}: {e}") from None
    try:
        hello, rest = _read_line(sock)
        if hello.get("relay") != "sdr-tools-relay":
            raise RelayError("that isn't an SDR Tools Relay")
        if hello.get("busy"):
            raise Busy(f"{hello.get('name', 'The relay')} is in use by someone else")
        if hello.get("password") and not password:
            raise RelayError(f"{hello.get('name', 'The relay')} needs a password")
        sock.sendall(json.dumps({"client": "SDR Toolkit", "version": __version__, "check": check,
                                 "auth": auth(password, hello.get("nonce", ""))}).encode() + b"\n")
        if rest:
            raise RelayError("unexpected data from the relay")
        answer, rest = _read_line(sock)
        if not answer.get("ok"):
            error = answer.get("error", "refused")
            raise (Busy if error == "busy" else RelayError)(f"{hello.get('name', 'The relay')}: {error}")
        sock.settimeout(None)
        return sock, hello, answer, rest
    except (OSError, RelayError) as e:
        sock.close()
        if isinstance(e, RelayError):
            raise
        raise RelayError(f"{host}:{port}: {e}") from None


# ---- finding relays ------------------------------------------------------

def _qt_broadcasts() -> list[str]:
    """Each IPv4 interface's broadcast address from Qt (Windows has no `ip`)."""
    try:
        from PyQt6.QtNetwork import QAbstractSocket, QNetworkInterface
    except ImportError:
        return []
    out = []
    for iface in QNetworkInterface.allInterfaces():
        flags = iface.flags()
        if not (flags & QNetworkInterface.InterfaceFlag.IsUp) or flags & QNetworkInterface.InterfaceFlag.IsLoopBack:
            continue
        for entry in iface.addressEntries():
            if entry.ip().protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol and not entry.broadcast().isNull():
                out.append(entry.broadcast().toString())
    return out


def broadcast_addresses() -> list[str]:
    """255.255.255.255 and each IPv4 interface's broadcast address (from `ip`, or Qt's list on Windows)."""
    out = ["255.255.255.255"]
    if sys.platform == "win32":
        return list(dict.fromkeys(out + _qt_broadcasts()))
    try:
        data = json.loads(subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True,
                                         timeout=3).stdout or "[]")
        for iface in data:
            for a in iface.get("addr_info", []):
                if iface.get("ifname") == "lo":
                    continue
                net = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network
                if net.prefixlen < 31:
                    out.append(str(net.broadcast_address))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return list(dict.fromkeys(out))


def discover(timeout=1.5, targets=None, port=DISCOVERY_PORT) -> list[dict]:
    """Relays on the LAN: their hello plus "host" and "port"."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    found = {}
    try:
        for addr in targets or broadcast_addresses():
            try:
                s.sendto(DISCOVER, (addr, port))
            except OSError as e:
                log.info("relay scan to %s: %s", addr, e)
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            s.settimeout(left)
            try:
                data, (host, _) = s.recvfrom(8192)
            except socket.timeout:
                break
            try:
                info = json.loads(data)
            except ValueError:
                continue
            if info.get("relay") == "sdr-tools-relay":
                info["host"] = host
                found[(host, info.get("port"))] = info
    finally:
        s.close()
    return sorted(found.values(), key=lambda r: r.get("name", "").lower())


def directory(url=DIRECTORY, timeout=10) -> list[dict]:
    """Relays listed on ssjrocks.com."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("relays", [])


def is_lan(host) -> bool:
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (OSError, ValueError):
        return False
    if ip in ipaddress.ip_network("100.64.0.0/10"):       # Tailscale / carrier-grade NAT
        return False
    return ip.is_private or ip.is_loopback


# ---- the bridge ----------------------------------------------------------

class Bridge:
    """A local rtl_tcp server that hands each connection on to the relay."""

    def __init__(self, host, port, password="", name=""):
        self.host, self.port, self.password = host, int(port), password
        self.name = name or f"{host}:{port}"
        self.relay_version = ""
        self.features: list[str] = []
        self.local_port = 0
        self.last_error = ""
        self.relay_ppm = 0
        self.connected = False
        self.bytes = 0
        self.device: dict = {}               # what the relay serves ({"type": "rtl"} or the HackRF's details)
        self.hackrf_settings: dict = {}      # amp, antenna, filter: sent to a HackRF at each session's start
        self._sock = None
        self._halt = threading.Event()
        self._active: list[socket.socket] = []

    @property
    def on_lan(self) -> bool:
        """The relay is on this network (a private LAN address, or this PC), so at your location.
        Tailscale (100.64.0.0/10) and internet addresses could be anywhere."""
        return is_lan(self.host)

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(2)
        s.settimeout(0.5)
        self._sock = s
        self.local_port = s.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True, name="relay-bridge").start()

    def stop(self):
        self._halt.set()
        for c in list(self._active):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._sock is not None:
            self._sock.close()

    def _accept(self):
        while not self._halt.is_set():
            try:
                local, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(local,), daemon=True).start()

    def _serve(self, local):
        # "In use" right after a decoder restarts (or the password check) is usually the previous
        # connection still closing the dongle: try again for a few seconds.
        end = time.monotonic() + BUSY_RETRY
        while True:
            try:
                remote, hello, answer, rest = open_session(self.host, self.port, self.password)
                break
            except Busy as e:
                if time.monotonic() < end and not self._halt.is_set():
                    time.sleep(0.5)
                    continue
                self.last_error = str(e)
            except RelayError as e:
                self.last_error = str(e)
            log.info("relay: %s", self.last_error)
            local.close()
            return
        self.last_error = ""
        self.relay_ppm = answer.get("ppm", 0)
        self.device = answer.get("device") or {"type": "rtl"}
        self.connected = True
        self._active += [local, remote]
        remote.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        link = HackRFLink(self.hackrf_settings) if self.device.get("type") == "hackrf" else None
        if link is not None:
            remote.sendall(hackrf_commands(self.hackrf_settings))
        if rest:
            local.sendall(link.incoming(rest) if link else rest)

        def pump(src, dst, count):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    if count:
                        self.bytes += len(data)
                    if link is not None:
                        data = link.incoming(data) if count else link.outgoing(data)
                    if data:
                        dst.sendall(data)
            except OSError:
                pass
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        t = threading.Thread(target=pump, args=(local, remote, False), daemon=True)
        t.start()
        pump(remote, local, True)
        t.join(2)
        for s in (local, remote):
            s.close()
            if s in self._active:
                self._active.remove(s)
        self.connected = False


_bridge: Bridge | None = None


def current() -> Bridge | None:
    """The bridge to the connected relay, if any (radio modes use the relay instead of a dongle)."""
    return _bridge


def connect(host, port, password="", name="") -> Bridge:
    """Check the relay answers and the password is right, then start the bridge to it."""
    global _bridge
    sock, hello, answer, _ = open_session(host, port, password, check=True)
    sock.close()
    disconnect()
    b = Bridge(host, port, password, name or hello.get("name", ""))
    b.relay_ppm = answer.get("ppm", 0)
    b.relay_version = str(hello.get("version", ""))
    b.features = list(hello.get("features") or [])
    b.devices = list(hello.get("devices") or [])
    b.device_id = str(hello.get("device") or "")
    chosen = next((d for d in b.devices if d.get("id") == b.device_id), b.devices[0] if b.devices else {})
    b.device = {"type": chosen.get("type", "rtl"), "name": chosen.get("name", "")}
    b.start()
    _bridge = b
    return b


def disconnect():
    global _bridge
    if _bridge is not None:
        _bridge.stop()
        _bridge = None


# ---- updating a relay -----------------------------------------------------

def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in str(v).lstrip("v").split("."))


def latest_relay_version(url=RELAY_RELEASES, timeout=10) -> str:
    """The newest SDR Tools Relay release on GitHub, e.g. "0.1.2"."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["tag_name"].lstrip("v")


def hello(host, port, timeout=5.0) -> dict:
    """The relay's hello (it's in use or not, its version), without logging in."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            return _read_line(sock)[0]
    except OSError as e:
        raise RelayError(f"Couldn't reach {host}:{port}: {e}") from None


def request_update(host, port, password) -> str:
    """Ask the relay to install its latest release (it needs the password). Returns the version
    it's installing; raises RelayError if it won't. It then restarts by itself."""
    end = time.monotonic() + BUSY_RETRY
    while True:
        try:
            sock = socket.create_connection((host, int(port)), timeout=15)
        except OSError as e:
            raise RelayError(f"Couldn't reach {host}:{port}: {e}") from None
        try:
            sock.settimeout(60)                     # the relay looks for the release before answering
            info, _ = _read_line(sock)
            if "update" not in (info.get("features") or []):
                raise RelayError(f"{info.get('name', 'The relay')} {info.get('version', '')} can't be updated "
                                 "from here: update it on its PC (from 0.1.2 on it can be)")
            if info.get("busy"):
                if time.monotonic() < end:
                    time.sleep(0.5)
                    continue
                raise Busy(f"{info.get('name', 'The relay')} is in use by someone else")
            sock.sendall(json.dumps({"client": "SDR Toolkit", "version": __version__, "update": True,
                                     "auth": auth(password, info.get("nonce", ""))}).encode() + b"\n")
            answer, _ = _read_line(sock)
            if not answer.get("ok"):
                raise RelayError(f"{info.get('name', 'The relay')}: {answer.get('error', 'refused')}")
            return str(answer.get("updating", ""))
        except OSError as e:
            raise RelayError(f"{host}:{port}: {e}") from None
        finally:
            sock.close()


def diagnose(host, port, password) -> dict:
    """The relay's dongle check (it needs the password and the relay free); takes ~20 s."""
    try:
        sock = socket.create_connection((host, int(port)), timeout=15)
    except OSError as e:
        raise RelayError(f"Couldn't reach {host}:{port}: {e}") from None
    try:
        sock.settimeout(90)
        info, _ = _read_line(sock)
        if "diagnose" not in (info.get("features") or []):
            raise RelayError(f"{info.get('name', 'The relay')} {info.get('version', '')} has no dongle check: "
                             "update it (0.1.3 or later)")
        if info.get("busy"):
            raise Busy(f"{info.get('name', 'The relay')} is in use: stop the RTL-SDR mode first")
        sock.sendall(json.dumps({"client": "SDR Toolkit", "version": __version__, "diagnose": True,
                                 "auth": auth(password, info.get("nonce", ""))}).encode() + b"\n")
        answer, _ = _read_line(sock, limit=1 << 20)
        if not answer.get("ok"):
            raise RelayError(f"{info.get('name', 'The relay')}: {answer.get('error', 'refused')}")
        return answer.get("report", {})
    except OSError as e:
        raise RelayError(f"{host}:{port}: {e}") from None
    finally:
        sock.close()


def stats(host, port, password, timeout=3.0) -> tuple[dict | None, float | None]:
    """The relay's links while it's in use (relays from 0.2.0): (its stats, the request's round trip in
    s), or (None, round trip) when nobody is connected; (None, None) if it can't say."""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError:
        return None, None
    try:
        info, _ = _read_line(sock)
        t0 = time.monotonic()
        sock.sendall(json.dumps({"client": "SDR Toolkit", "version": __version__, "stats": True,
                                 "auth": auth(password, info.get("nonce", ""))}).encode() + b"\n")
        answer, _ = _read_line(sock)
        rtt = time.monotonic() - t0
        return (answer.get("stats") if answer.get("ok") else None), rtt
    except (OSError, RelayError):
        return None, None
    finally:
        sock.close()


def wait_for_version(host, port, version, timeout=300, every=5.0) -> bool:
    """Wait for the relay to come back with *version* (it restarts after updating)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if str(hello(host, port).get("version")) == version:
                return True
        except RelayError:
            pass
        time.sleep(every)
    return False
