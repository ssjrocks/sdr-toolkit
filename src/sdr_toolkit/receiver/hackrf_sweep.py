"""The survey on a relay's HackRF: its own sweep mode (hackrf_sweep), which hops 20 MHz at a time across
the range at several GHz a second, instead of tuning slice by slice.

The relay sends hackrf_sweep's blocks (each 0x7F 0x7F, the frequency as u64, then signed 8-bit IQ;
relays from 0.3.0 cut each to the samples the FFT reads). Each block is done as hackrf_sweep does it:
an FFT of its last N samples (a Hann window, scaled by 1/N), of which two quarter-bands are kept, one
each side of the centre and clear of its DC spike: [f, f + 5 MHz] from bins 1 + 5N/8 on and
[f + 10, f + 15 MHz] from bins 1 + N/8 on (f is the block's frequency). The interleaved tunings fill
the gaps between them.

It has the same face as survey.Survey for the receiver's survey events (lo, hi, rate, fft, done,
current, last_pass, centers), and runs on its own connection to the relay: the receiver's stream is
closed meanwhile (a relay serves one connection at a time)."""

import collections
import logging
import math
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

RATE = 20_000_000                  # hackrf_sweep's sample rate and 20 MHz tuning step
BLOCK = 16384                      # its bytes per block
FFT = 1024                         # bins per block: 19.5 kHz each, 2 kB a block from a relay that trims
MIN_HZ, MAX_HZ = 1e6, 6000e6


def block_power(iq: np.ndarray, window: np.ndarray) -> np.ndarray:
    """hackrf_sweep's pwr[] for blocks of signed 8-bit IQ (rows: blocks, the last N samples each)."""
    n = window.size
    x = (iq[:, 0::2].astype(np.float32) + 1j * iq[:, 1::2].astype(np.float32)) * (window / 128.0)
    f = np.fft.fft(x, axis=1) / n
    return 10 * np.log10(np.abs(f) ** 2 + 1e-20)


def quarter_bands(pwr: np.ndarray, freq: int) -> list[tuple[float, np.ndarray]]:
    """The two quarter-bands hackrf_sweep writes for one block: [(the first bin's frequency, dB)]. It labels
    them from the block's frequency, but its bins start one on ("1 +"), so each sits one bin higher."""
    n = len(pwr)
    res = RATE / n
    return [(float(freq) + res, pwr[1 + 5 * n // 8: 1 + 5 * n // 8 + n // 4]),
            (float(freq + RATE // 2) + res, pwr[1 + n // 8: 1 + n // 8 + n // 4])]


class HackRFSweep:
    def __init__(self, bridge, lo, hi, settings=None, fft=FFT):
        self.bridge = bridge
        self.settings = dict(settings or {})      # lna, vga, amp, antenna
        self.rate, self.fft = RATE, int(fft)
        self.min_hz, self.max_hz = MIN_HZ, MAX_HZ
        self.window = (0.5 * (1 - np.cos(2 * np.pi * np.arange(self.fft) / (self.fft - 1)))).astype(np.float32)
        self.done: collections.deque = collections.deque(maxlen=20000)
        self.current = None
        self.last_pass = 0.0
        self.error = ""
        self.blocks = 0
        self.lo = self.hi = 0.0
        self.centers: list[float] = []
        self._sock = None
        self._thread = None
        self._halt = threading.Event()
        self._want = None
        self.set_view(lo, hi)

    # ---- the receiver's side ---------------------------------------------------------------------

    def set_view(self, lo, hi):
        """Sweep [lo, hi] (whole MHz, at least one 20 MHz step): the relay's sweep starts again on it."""
        lo = max(float(lo), self.min_hz)
        hi = min(max(float(hi), lo + RATE), self.max_hz)
        ranges = (int(lo // 1e6), int(math.ceil(hi / 1e6)))
        self.lo, self.hi = lo, hi
        steps = max(1, math.ceil((ranges[1] - ranges[0]) / 20))
        self.centers = [ranges[0] * 1e6 + RATE * (k + 0.5) for k in range(steps)]
        if ranges != self._want:
            self._want = ranges
            self._restart()

    def stop(self):
        self._halt.set()
        self._close()
        if self._thread is not None:
            self._thread.join(3)

    # ---- the connection --------------------------------------------------------------------------

    def _close(self):
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _restart(self):
        self._close()
        if self._thread is None or not self._thread.is_alive():
            self._halt.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="hackrf-sweep")
            self._thread.start()

    def _run(self):
        from .. import relay
        while not self._halt.is_set():
            ranges = self._want
            trim = "sweep_trim" in self.bridge.features
            extra = {"stream": "sweep", "ranges": [list(ranges)],
                     "lna": self.settings.get("lna", 16), "vga": self.settings.get("vga", 20)}
            for k in ("amp", "antenna"):
                if self.settings.get(k) is not None:
                    extra[k] = self.settings[k]
            if trim:
                extra["fft_size"] = self.fft
            try:
                sock, _, answer, rest = relay.open_session(self.bridge.host, self.bridge.port, self.bridge.password,
                                                           device=self.bridge.want_device, extra=extra)
            except relay.Busy:
                time.sleep(0.5)                       # the receiver's own connection still closing
                continue
            except relay.RelayError as e:
                self.error = str(e)
                log.info("HackRF sweep: %s", e)
                return
            self.error = ""
            self._sock = sock
            first = (answer.get("ranges") or [ranges[0]])[0]            # where the relay starts each pass
            self._read(sock, rest, 10 + 2 * self.fft if trim else BLOCK, ranges, first * 1_000_000)
            if self._want == ranges:                  # it ended by itself (not a new range): give up
                return

    def _read(self, sock, buf, size, ranges, first):
        pass_start = time.monotonic()
        buf = bytearray(buf)
        try:
            while not self._halt.is_set() and self._want == ranges:
                data = sock.recv(1 << 18)
                if not data:
                    return
                buf += data
                n = len(buf) // size
                if not n:
                    continue
                blocks = np.frombuffer(bytes(buf[: n * size]), np.uint8).reshape(n, size)
                del buf[: n * size]
                good = (blocks[:, 0] == 0x7F) & (blocks[:, 1] == 0x7F)
                blocks = blocks[good]
                if not len(blocks):
                    continue
                freqs = blocks[:, 2:10].copy().view("<u8")[:, 0]
                iq = blocks[:, size - 2 * self.fft:].view(np.int8)
                pwr = block_power(iq, self.window)
                for f, p in zip(freqs, pwr):
                    if int(f) == first and self.blocks:
                        now = time.monotonic()
                        self.last_pass, pass_start = now - pass_start, now
                    self.done.extend(quarter_bands(p, int(f)))
                    self.current = float(f) + RATE / 2
                    self.blocks += 1
        except OSError:
            pass
