"""ISM across a whole band at once (a HackRF): the engine cuts a wide stream into channels and runs rtl_433 on
each. Against a stand-in rtl_tcp server streaming a generated band with a sensor in it."""

import json
import shutil
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import radio_signals  # noqa: E402
from sdr_toolkit import system  # noqa: E402
from sdr_toolkit.engines import ism_wide  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("rtl_433"), reason="rtl_433 isn't installed")


def test_bands_are_planned_and_split():
    rate, centre, chans = ism_wide.plan(915e6, 928e6)
    assert rate == 19_200_000 and centre == 921.5e6 and len(chans) == 13
    assert all(b - a <= 1e6 + 1 for a, b in zip(chans, chans[1:]))
    assert ism_wide.plan(433.05e6, 434.79e6)[0] == 2_400_000
    assert ism_wide.split([(902e6, 928e6)]) == [(902e6, 915e6), (915e6, 928e6)]


def serve(data: bytes):
    """An rtl_tcp server sending `data` once to the first client (and the commands it got)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = bytearray()

    def commands(c):
        try:
            while chunk := c.recv(4096):
                got.extend(chunk)
        except OSError:
            pass

    def run():
        c, _ = srv.accept()
        threading.Thread(target=commands, args=(c,), daemon=True).start()
        c.sendall(b"RTL0" + struct.pack(">II", 5, 29))
        step = 1 << 20
        for i in range(0, len(data), step):
            c.sendall(data[i:i + step])
        threading.Event().wait(3)                  # rtl_433 finishing
        c.close()
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], got


def test_a_sensor_anywhere_in_the_band_is_decoded(tmp_path):
    rate, centre = 19_200_000, 921.5e6
    f = 918.6e6
    x = radio_signals.nexus_iq(0xA5, 2, 21.7, 58, rate=rate, offset_hz=f - centre)
    x = np.concatenate([np.zeros(rate // 2, np.complex64), x.astype(np.complex64)])     # the settle time
    b = np.empty(2 * len(x), np.uint8)
    b[0::2] = np.clip(np.rint(x.real * 127.5 + 127.5), 0, 255)
    b[1::2] = np.clip(np.rint(x.imag * 127.5 + 127.5), 0, 255)
    port, got = serve(b.tobytes())
    p = subprocess.run(system.python_command("sdr_toolkit.engines.ism_wide", "--port", str(port), "--bands",
                                             "915000000-928000000", "--", "-F", "json", "-M", "level"),
                       capture_output=True, text=True, timeout=120)
    events = [json.loads(ln) for ln in p.stdout.splitlines() if ln.startswith("{")]
    band = next(e for e in events if e.get("event") == "band")
    assert band["rate"] == rate and len(band["channels"]) == 13
    nexus = [e for e in events if str(e.get("model", "")).startswith("Nexus")]
    assert nexus, p.stdout[-2000:] + p.stderr[-2000:]
    assert all(e["temperature_C"] == 21.7 and abs(e["freq"] - 918.6) < 0.15 for e in nexus), nexus[:2]
    cmds = [struct.unpack(">BI", bytes(got[i:i + 5])) for i in range(0, len(got) // 5 * 5, 5)]
    assert (2, rate) in cmds and (1, int(centre)) in cmds
