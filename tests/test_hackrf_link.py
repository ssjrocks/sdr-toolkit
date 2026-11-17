"""A relay's HackRF on the toolkit's side: the bridge's link (gain from the HackRF bar, live changes to every
stream, narrowing and the DC spike done here for a relay before 0.3.0), the automatic gain, and the
survey on the HackRF's own sweep. Against a stand-in relay that records what it's sent."""

import json
import socket
import struct
import threading
import time

import numpy as np

from sdr_toolkit import hackrf_agc, relay
from sdr_toolkit.receiver import hackrf_sweep

HACKRF_ID = "hackrf:0000000000000000901868dc283c918b"
NEW = ["check", "diagnose", "hackrf", "sweep", "narrow", "dc_block", "levels", "sweep_trim"]
OLD = ["check", "diagnose", "hackrf", "sweep"]


def cmds(data: bytes) -> list[tuple[int, int]]:
    return [struct.unpack(">BI", data[i:i + 5]) for i in range(0, len(data) // 5 * 5, 5)]


class FakeRelay:
    """Enough of SDR Tools Relay for the bridge: the hello, the answer, rtl_tcp's header and `samples` over
    and over, recording every command (and each sweep request) it's sent."""

    def __init__(self, features, samples=bytes([128, 128]) * 4096, sweep_blocks=b""):
        self.features, self.samples, self.sweep_blocks = features, samples, sweep_blocks
        self.received = bytearray()
        self.replies: list[dict] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.halt = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.halt.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        hello = {"relay": "sdr-tools-relay", "protocol": 1, "name": "Fake", "version": "0.3.0",
                 "devices": [{"index": 0, "id": HACKRF_ID, "type": "hackrf", "name": "HackRF One"}],
                 "password": False, "busy": False, "features": self.features, "device": "rtl:0", "nonce": "n"}
        c.sendall(json.dumps(hello).encode() + b"\n")
        line = b""
        while not line.endswith(b"\n"):
            line += c.recv(1)
        reply = json.loads(line)
        self.replies.append(reply)
        if reply.get("check") or reply.get("stats"):
            c.sendall(json.dumps({"ok": True, "stats": None}).encode() + b"\n")
            c.close()
            return
        if reply.get("stream") == "sweep":
            c.sendall(json.dumps({"ok": True, "stream": "sweep", "ranges": reply["ranges"][0]}).encode() + b"\n")
            try:
                while not self.halt.is_set():
                    c.sendall(self.sweep_blocks)
                    time.sleep(0.02)
            except OSError:
                pass
            return
        c.sendall(json.dumps({"ok": True, "ppm": 0, "device": {"type": "hackrf"}}).encode() + b"\n")
        c.sendall(b"RTL0" + struct.pack(">II", 100, 52))
        c.settimeout(0.01)
        try:
            while not self.halt.is_set():
                c.sendall(self.samples)
                try:
                    got = c.recv(4096)
                    if not got:
                        return
                    self.received += got
                except socket.timeout:
                    pass
        except OSError:
            pass

    def close(self):
        self.halt.set()
        self.sock.close()


def stream_through(b, send=b"", seconds=0.5):
    """A decoder on the bridge: sends `send`, returns (header, samples received)."""
    s = socket.create_connection(("127.0.0.1", b.local_port), timeout=5)
    s.sendall(send)
    data = b""
    end = time.time() + seconds
    while time.time() < end:
        try:
            data += s.recv(65536)
        except socket.timeout:
            break
    return s, data[:12], data[12:]


def test_the_bars_settings_reach_every_stream_live():
    fake = FakeRelay(NEW)
    try:
        b = relay.connect("127.0.0.1", fake.port)
        b.apply_hackrf({"lna": 24, "vga": 20, "amp": 0, "antenna": 1, "filter": 0})
        s, head, _ = stream_through(b, relay.command(3, 0) + relay.command(4, 400))   # the decoder's own gain
        assert head[:4] == b"RTL0" and struct.unpack(">I", head[4:8])[0] == relay.R820T
        got = cmds(bytes(fake.received))
        # At the start: the bar's settings; the decoder's gain commands became the bar's LNA and VGA.
        assert got[:5] == [(relay.SET_AMP, 0), (relay.SET_ANTENNA_POWER, 1), (relay.SET_BASEBAND_FILTER, 0),
                           (relay.SET_LNA_GAIN, 24), (relay.SET_VGA_GAIN, 20)]
        assert got[5:] == [(relay.SET_LNA_GAIN, 24), (relay.SET_VGA_GAIN, 20)] * 2 and (3, 0) not in got
        # Changed on the bar while it streams: sent now, and the decoder's next gain command keeps it.
        n = len(fake.received)
        b.apply_hackrf({"lna": 32})
        s.sendall(relay.command(4, 100))
        time.sleep(0.3)
        got = cmds(bytes(fake.received[n:]))
        assert got == [(relay.SET_LNA_GAIN, 32), (relay.SET_LNA_GAIN, 32), (relay.SET_VGA_GAIN, 20)]
        s.close()
        # The device asked for: the HackRF the relay has (its window is on an RTL-SDR that isn't there).
        assert fake.replies[-1]["device"] == HACKRF_ID
    finally:
        relay.disconnect()
        fake.close()


def test_a_new_relay_narrows_and_takes_the_spike_out_itself():
    fake = FakeRelay(NEW, samples=bytes([168, 108]) * 4096)          # I +40, Q -20: a DC offset
    try:
        b = relay.connect("127.0.0.1", fake.port)
        s, _, data = stream_through(b, relay.command(relay.SET_RATE, 250_000))
        assert (relay.SET_RATE, 250_000) in cmds(bytes(fake.received))      # asked as it is
        assert data[:8] == bytes([168, 108]) * 4                             # and passed on untouched
        s.close()
    finally:
        relay.disconnect()
        fake.close()


def test_an_older_relay_gets_a_multiple_and_its_spike_is_taken_out_here():
    fake = FakeRelay(OLD, samples=bytes([168, 108]) * 4096)
    try:
        b = relay.connect("127.0.0.1", fake.port)
        s, _, data = stream_through(b, relay.command(relay.SET_RATE, 250_000), seconds=1.0)
        assert (relay.SET_RATE, 2_000_000) in cmds(bytes(fake.received))
        x = np.frombuffer(data[len(data) // 2 // 2 * 2:], np.uint8).astype(float) - 127.5
        assert len(x) and abs(x[0::2].mean()) < 0.1 and abs(x[1::2].mean()) < 0.1
        level = b.local_level()
        assert level and level["peak"] >= 40 and -12 < level["dbfs"] < 0
        s.close()
    finally:
        relay.disconnect()
        fake.close()


def test_automatic_gain():
    assert hackrf_agc.split(40) == (32, 8) and hackrf_agc.split(20) == (8, 12) and hackrf_agc.split(102) == (40, 62)
    a = hackrf_agc.AGC(16, 20)
    assert a.step({"dbfs": -36, "clipped": 0}) == (32, 14)            # far too quiet: 10 dB up
    assert a.step({"dbfs": -30, "clipped": 0}) is None                # then it waits for the level to follow
    assert a.step({"dbfs": -30, "clipped": 0}) is None
    assert a.step({"dbfs": -30, "clipped": 0}) == (32, 20)            # still quiet: 6 dB
    assert a.step({"dbfs": -5, "clipped": 0.01}) == (32, 12)          # clipping: 8 dB down, no waiting
    a = hackrf_agc.AGC(32, 20)
    assert a.step({"dbfs": -20, "clipped": 0}) is None                # in the band: left alone
    assert a.step({"dbfs": -12, "clipped": 0}) == (32, 16)            # a bit hot: 4 dB
    a = hackrf_agc.AGC(32, 44)
    assert a.step({"dbfs": -50, "clipped": 0}) is None                # at its ceiling: no more


def test_sweep_bins_are_hackrf_sweeps():
    n = hackrf_sweep.FFT
    window = (0.5 * (1 - np.cos(2 * np.pi * np.arange(n) / (n - 1)))).astype(np.float32)
    f = 2_400_000_000
    # The HackRF tuned 7.5 MHz above the block's frequency: a tone 1 MHz above f is 6.5 MHz below centre,
    # and one 12 MHz above f is 4.5 MHz above it.
    t = np.arange(n) / hackrf_sweep.RATE
    z = 60 * np.exp(2j * np.pi * -6.5e6 * t) + 60 * np.exp(2j * np.pi * 4.5e6 * t)
    iq = np.empty(2 * n, np.int8)
    iq[0::2], iq[1::2] = np.rint(z.real), np.rint(z.imag)
    pwr = hackrf_sweep.block_power(iq[None, :], window)[0]
    (lo1, b1), (lo2, b2) = hackrf_sweep.quarter_bands(pwr, f)
    res = hackrf_sweep.RATE / n
    assert lo1 == f + res and lo2 == f + 10e6 + res and len(b1) == len(b2) == n // 4
    assert abs(lo1 + np.argmax(b1) * res - (f + 1e6)) <= res / 2 and abs(lo2 + np.argmax(b2) * res - (f + 12e6)) <= res / 2


def test_survey_on_the_hackrfs_sweep():
    n = hackrf_sweep.FFT
    blocks = b""
    for f in (2_400_000_000, 2_405_000_000):
        block = bytes([0x7F, 0x7F]) + f.to_bytes(8, "little") + np.zeros(2 * n, np.int8).tobytes()
        blocks += block
    fake = FakeRelay(NEW, sweep_blocks=blocks)
    try:
        b = relay.connect("127.0.0.1", fake.port)
        sw = hackrf_sweep.HackRFSweep(b, 2400e6, 2500e6, {"lna": 24, "vga": 16, "antenna": 1})
        end = time.time() + 5
        while time.time() < end and len(sw.done) < 8:
            time.sleep(0.05)
        sw.stop()
        req = next(r for r in fake.replies if r.get("stream") == "sweep")
        assert req["ranges"] == [[2400, 2500]] and req["fft_size"] == n and req["lna"] == 24 and req["antenna"] == 1
        res = hackrf_sweep.RATE / n
        los = {lo - res for lo, _ in list(sw.done)}
        assert {2400e6, 2410e6, 2405e6, 2415e6} <= los and sw.rate / sw.fft == res
        assert sw.last_pass > 0
    finally:
        relay.disconnect()
        fake.close()


def test_spike_removal_keeps_i_and_q_apart_in_odd_sized_chunks():
    # The stream arrives in TCP chunks of any length (odd ones on Windows): a byte left over must start
    # the next chunk as the Q it is, not be taken for an I.
    dc = relay.DCBlock()
    data = bytes([168, 108]) * 50000                             # I +40.5, Q -19.5 from 127.5
    out, i = b"", 0
    for size in [4097, 3, 8191, 1, 65535, 2] * 20:
        out += dc(data[i:i + size], 2_000_000)
        i += size
    x = np.frombuffer(out[: len(out) // 2 * 2], np.uint8).astype(float) - 127.5
    assert abs(x[0::2].mean()) < 0.05 and abs(x[1::2].mean()) < 0.05
