"""Where the receiver's samples come from: an rtl_tcp server, or a recording.

A dongle on this PC is driven through the real rtl_tcp (from the rtl-sdr package), run on
127.0.0.1; a dongle on another PC through SDR Tools Relay's bridge, which is rtl_tcp too.
Every setting is an rtl_tcp command, the same either way.
"""

import collections
import logging
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from .. import system

log = logging.getLogger(__name__)

SET_FREQ, SET_RATE, SET_GAIN_MODE, SET_GAIN, SET_PPM, SET_IF_GAIN, SET_TEST_MODE, SET_AGC, \
    SET_DIRECT_SAMPLING, SET_OFFSET_TUNING, SET_RTL_XTAL, SET_TUNER_XTAL, SET_GAIN_INDEX, SET_BIAS_TEE = range(1, 15)
TUNER_NAMES = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}
MAX_BUFFERED = 2.0            # s of samples kept if processing falls behind; older ones are dropped


class SourceError(RuntimeError):
    pass


class RtlTcp:
    """An rtl_tcp client: commands out, samples in (a reading thread fills a bounded buffer)."""

    def __init__(self, host, port, timeout=10.0):
        try:
            self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as e:
            raise SourceError(f"Couldn't connect to rtl_tcp at {host}:{port}: {e}") from None
        head = b""
        try:
            while len(head) < 12:
                chunk = self.sock.recv(12 - len(head))
                if not chunk:
                    raise SourceError("the rtl_tcp server closed the connection (the dongle may be in use)")
                head += chunk
        except OSError as e:
            raise SourceError(f"rtl_tcp at {host}:{port}: {e}") from None
        if head[:4] != b"RTL0":
            raise SourceError("that isn't an rtl_tcp server")
        self.tuner, self.gain_count = struct.unpack(">II", head[4:12])
        self.sock.settimeout(1.0)
        self.rate = 2048000
        self._chunks: collections.deque = collections.deque()
        self._size = 0
        self._lock = threading.Lock()
        self.dropped = 0
        self.received = 0
        self.last_data = time.monotonic()
        self.error = ""
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True, name="rtl_tcp-read")
        self._thread.start()

    @property
    def tuner_name(self):
        return TUNER_NAMES.get(self.tuner, f"tuner {self.tuner}")

    def command(self, cmd, value):
        try:
            self.sock.sendall(struct.pack(">BI", cmd, int(value) & 0xFFFFFFFF))
        except OSError as e:
            self.error = f"rtl_tcp: {e}"

    def set_rate(self, rate):
        self.rate = int(rate)
        self.command(SET_RATE, rate)
        with self._lock:                       # samples at the old rate would be misread
            self._chunks.clear()
            self._size = 0

    def _read(self):
        while not self._halt.is_set():
            try:
                data = self.sock.recv(1 << 18)
            except socket.timeout:
                continue
            except OSError as e:
                self.error = f"rtl_tcp: {e}"
                break
            if not data:
                self.error = "the rtl_tcp server closed the connection"
                break
            self.received += len(data)
            self.last_data = time.monotonic()
            with self._lock:
                self._chunks.append(data)
                self._size += len(data)
                limit = int(self.rate * 2 * MAX_BUFFERED)
                while self._size > limit and len(self._chunks) > 1:
                    self._size -= len(self._chunks.popleft())
                    self.dropped += 1

    def read(self) -> bytes:
        """Everything received since the last call (an even number of bytes)."""
        with self._lock:
            data = b"".join(self._chunks)
            self._chunks.clear()
            self._size = 0
        if len(data) % 2:
            with self._lock:
                self._chunks.appendleft(data[-1:])
                self._size = 1
            data = data[:-1]
        return data

    def close(self):
        self._halt.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(2)


class LocalRtlTcp(RtlTcp):
    """This PC's dongle, through rtl_tcp run here on a free local port."""

    def __init__(self, device="0", rate=2400000, freq=100000000):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        cmd = ["rtl_tcp", "-a", "127.0.0.1", "-p", str(port), "-d", str(device).lstrip(":") or "0",
               "-s", str(int(rate)), "-f", str(int(freq))]
        log.info("starting %s", " ".join(cmd))
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            raise SourceError("rtl_tcp isn't installed (the rtl-sdr package)") from None
        self.output: list[str] = []
        ready = threading.Event()

        def watch():
            for line in self.proc.stdout:
                line = line.rstrip()
                self.output.append(line)
                del self.output[:-40]
                log.info("rtl_tcp: %s", line)
                if "listening" in line.lower():
                    ready.set()
            ready.set()

        threading.Thread(target=watch, daemon=True, name="rtl_tcp-log").start()
        ready.wait(10)
        if self.proc.poll() is not None or not any("listening" in x.lower() for x in self.output):
            text = "\n".join(self.output).lower()
            self.stop_server()
            if "no supported devices" in text or "no matching devices" in text:
                raise SourceError("No RTL-SDR found: plug it in, or connect a relay (Connect relay…)." + system.DRIVER_HINT)
            if "usb_claim_interface" in text or "busy" in text:
                raise SourceError("The RTL-SDR is in use by another program (another mode, SDR++, GQRX…)")
            raise SourceError("rtl_tcp didn't start: " + " / ".join(self.output[-3:]))
        try:
            super().__init__("127.0.0.1", port)
        except SourceError:
            self.stop_server()
            raise
        self.rate = int(rate)

    def stop_server(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def close(self):
        super().close()
        self.stop_server()


class FileSource:
    """A .cu8 recording played in real time and looped; tuning commands only change the labels."""

    tuner, gain_count, tuner_name = 5, 29, "recording"

    def __init__(self, path, rate):
        p = Path(path).expanduser()
        if not p.is_file():
            raise SourceError("Choose a recording: Settings → Recording (.cu8, unsigned 8-bit IQ)")
        self.data = p.read_bytes()
        self.rate = int(rate)
        self.pos = 0
        self.t0 = time.monotonic()
        self.sent = 0
        self.dropped = self.received = 0
        self.error = ""
        self.last_data = time.monotonic()
        self.commands: list[tuple] = []

    def command(self, cmd, value):
        self.commands.append((cmd, value))

    def set_rate(self, rate):
        self.commands.append((SET_RATE, rate))      # the recording's rate doesn't change

    def read(self) -> bytes:
        due = int((time.monotonic() - self.t0) * self.rate) * 2 - self.sent
        due -= due % 2
        if due <= 0:
            return b""
        out = bytearray()
        while len(out) < due:
            take = min(due - len(out), len(self.data) - self.pos)
            out += self.data[self.pos:self.pos + take]
            self.pos = (self.pos + take) % len(self.data)
        self.sent += len(out)
        self.received += len(out)
        self.last_data = time.monotonic()
        return bytes(out)

    def close(self):
        pass
