"""Using an RTL-SDR on another PC through SDR Tools Relay (github.com/ssjrocks/sdr-tools-relay).

The relay protocol (version 1, the relay's sdr_relay/protocol.py has the full description):
the relay sends a JSON hello with a nonce, the client answers with HMAC-SHA256(password,
nonce), the relay says {"ok": true} and from then on speaks plain rtl_tcp.

While a relay is connected the window runs a ``Bridge``: a local rtl_tcp server on
127.0.0.1 that does the handshake for each decoder that connects and then passes the
bytes through. rtl_433, DSD-FME and GNSS-SDR connect to it as to any rtl_tcp server;
the others read relay_stream's output (python -m sdr_toolkit.relay_stream).
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import subprocess
import sys
import threading
import time
import urllib.request

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
        self.connected = True
        self._active += [local, remote]
        remote.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if rest:
            local.sendall(rest)

        def pump(src, dst, count):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
                    if count:
                        self.bytes += len(data)
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
