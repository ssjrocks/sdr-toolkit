"""--self-test RESULTS.json: checks a build works on this PC without a dongle, a sound card or a
user (the Windows CI runs it on the built app and again on the installed one).

Each check is recorded in RESULTS.json as ok or the error; the exit code is 0 only if every
required check passed. Optional ones (sound devices: CI machines have none) are only reported.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np

from . import __version__, system


def _morse():
    """Morse decoded through the mode interface, as the window runs it: numpy, scipy, the decoder."""
    from .engines.morse.synth import transmission
    from .modes import MODES
    mode = next(m for m in MODES if m.id == "morse")()
    audio = np.concatenate([np.zeros(4000, np.float32), transmission("CQ TEST DE VK2ABC", 25, 20).astype(np.float32),
                            np.zeros(24000, np.float32)])
    v = dict(mode.values)
    v.update(tone=700, speed="any")
    mode.start(v)
    events = []
    n = int(0.05 * mode.rate)
    for i in range(0, len(audio), n):
        events += mode.feed(audio[i:i + n], 1.8e9 + (i + n) / mode.rate)
        events += mode.poll()
    events += mode.stop()
    text = "\n".join(e.text for e in events if e.kind == "line")
    assert "CQ TEST DE VK2ABC" in text, f"decoded {text!r}"
    return "decoded CQ TEST DE VK2ABC"


def _receiver_dsp():
    """The receiver's demodulator on a synthetic NFM signal."""
    from .receiver import dsp
    rate = 960000
    t = np.arange(rate // 2) / rate
    x = (0.5 * np.exp(1j * (2 * np.pi * 100000 * t + 1.5 * np.sin(2 * np.pi * 1000 * t)))).astype(np.complex64)
    d = dsp.Demodulator(rate, "NFM", offset=100000)
    audio = d.process(x)
    spec = np.abs(np.fft.rfft(audio[len(audio) // 2:]))
    peak = np.argmax(spec[1:]) + 1
    f = peak * dsp.AUDIO_RATE / (len(audio) - len(audio) // 2)
    assert 900 < f < 1100, f"tone at {f:.0f} Hz"
    return f"1 kHz tone out of NFM ({f:.0f} Hz)"


def _helper():
    """The helper process the radio modes run (relay_stream) starts and answers."""
    r = subprocess.run(system.python_command("sdr_toolkit.relay_stream", "--help"), capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0 and "--freq" in r.stdout, f"exit {r.returncode}: {r.stdout[-300:]} {r.stderr[-300:]}"
    iss = ["1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927",
           "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537"]
    job = {"home": [-33.87, 151.21], "start": 1221955200.0, "days": 1, "min_el": 10, "sats": [[25544, *iss]]}
    r = subprocess.run(system.python_command("sdr_toolkit.satellites.worker"), input=json.dumps(job),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"satellite worker exit {r.returncode}: {r.stderr[-500:]}"
    norad, passes = json.loads(r.stdout.splitlines()[0])
    assert norad == 25544 and passes, f"no ISS passes: {r.stdout[:300]}"
    return f"relay_stream runs; the satellite worker found {len(passes)} ISS passes"


def _rtl_tools():
    """The RTL-SDR tools are found and start (with no dongle they say so)."""
    found = {t: shutil.which(t) for t in ("rtl_tcp", "rtl_fm", "rtl_sdr")}
    missing = [t for t, p in found.items() if not p]
    assert not missing, f"not found: {missing} (PATH starts {system.tool_dirs()[:2]})"
    r = subprocess.run([found["rtl_fm"], "-f", "100M", "-"], capture_output=True, text=True, timeout=30)
    out = (r.stdout + r.stderr).lower()
    assert "no supported devices" in out or "found" in out, f"rtl_fm said: {out[-300:]!r}"
    return {t: p for t, p in found.items()}


def _logbook():
    from .logbook import Logbook
    with tempfile.TemporaryDirectory() as d:
        book = Logbook(Path(d) / "test.sqlite")
        book.close()
    return "opened and closed"


def _window():
    """The main window builds (offscreen) and closes."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from . import gui
    from .gui import MainWindow

    class QuietCapture:
        """No sound card needed (CI machines have none): the window's own use of audio is checked
        by "sound outputs"; here only that it builds."""
        def __init__(self, target=None, rate=48000):
            self.rate = rate

        def start(self):
            pass

        def read(self):
            return np.zeros(0, np.float32)

        def stop(self):
            pass

    gui.AudioCapture = QuietCapture
    with tempfile.TemporaryDirectory() as d:
        w = MainWindow(Path(d))
        w.show()
        app.processEvents()
        title = w.windowTitle()
        w.close()
        app.processEvents()
    return title


def _sound():
    from .capture import list_outputs
    return [name for _, name in list_outputs()] or "no sound outputs on this PC"


CHECKS = [("morse decoder", _morse, True), ("receiver DSP", _receiver_dsp, True), ("helper processes", _helper, True),
          ("RTL-SDR tools", _rtl_tools, system.WINDOWS), ("logbook", _logbook, True), ("window", _window, True),
          ("sound outputs", _sound, False)]


def run(results_path: str) -> int:
    system.setup_environment()
    results = {"version": __version__, "platform": sys.platform, "frozen": system.FROZEN, "checks": {}}
    failed = []
    for name, fn, required in CHECKS:
        try:
            results["checks"][name] = {"ok": True, "result": fn()}
        except Exception as e:
            results["checks"][name] = {"ok": False, "required": required, "error": f"{type(e).__name__}: {e}",
                                       "trace": traceback.format_exc()[-2000:]}
            if required:
                failed.append(name)
    results["passed"] = not failed
    Path(results_path).write_text(json.dumps(results, indent=2, default=str))
    return 1 if failed else 0
