"""Records what is playing to an output device: on Linux via PipeWire's pw-record, on Windows
through WASAPI loopback (the soundcard package).

``stream.capture.sink=true`` records an output's monitor, not a microphone.
With no target it records the default output and follows it if it changes.
WASAPI's loopback is the same idea: what an output plays, resampled to the rate asked for.
"""

import json
import queue
import shutil
import subprocess
import threading

import numpy as np

from . import system


def list_outputs() -> list[tuple[str, str]]:
    """(id, description) of each audio output: PipeWire node names, or Windows' device ids."""
    if system.WINDOWS:
        return _windows_outputs()
    return _pipewire_outputs()


def _pipewire_outputs() -> list[tuple[str, str]]:
    """(node name, description) of each audio output, from pw-dump."""
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, timeout=5, check=True).stdout
        objs = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    sinks = []
    for o in objs:
        props = (o.get("info") or {}).get("props") or {}
        if props.get("media.class") == "Audio/Sink" and props.get("node.name"):
            sinks.append((props["node.name"], props.get("node.description") or props["node.name"]))
    return sorted(sinks, key=lambda s: s[1].lower())


class PipeWireCapture:
    """Stream an output's audio as mono float32 at *rate* from a pw-record subprocess."""

    def __init__(self, target: str | None = None, rate: int = 48000):
        self._target = target
        self.rate = rate
        self._read_bytes = rate // 10 * 2  # 0.1 s of s16 mono
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._chunks: queue.Queue[bytes] = queue.Queue()
        self._stopping = False

    def start(self) -> None:
        if not shutil.which("pw-record"):
            raise RuntimeError("pw-record not found. Install it with: sudo apt install pipewire-bin")
        cmd = [
            "pw-record",
            "-P", '{ stream.capture.sink=true node.name=sdr-toolkit media.name="SDR Toolkit" }',
            "--format", "s16",
            "--rate", str(self.rate),
            "--channels", "1",
            "--latency", "20ms",
            "--raw",        # without this, a header is written into the stream
        ]
        if self._target:
            cmd += ["--target", self._target]
        cmd.append("-")
        # pw-record writes through stdio, which buffers 4 KB (256 ms here) on
        # a pipe; unbuffered, audio arrives every ~10 ms.
        if shutil.which("stdbuf"):
            cmd = ["stdbuf", "-o0"] + cmd
        self._stopping = False
        self._process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        while True:
            data = self._process.stdout.read1(self._read_bytes)
            if not data:
                break
            self._chunks.put(data)

    def read(self) -> np.ndarray:
        """All audio captured since the last call (may be empty).

        Raises RuntimeError if pw-record has exited.
        """
        parts = []
        while True:
            try:
                parts.append(self._chunks.get_nowait())
            except queue.Empty:
                break
        if not parts:
            if self._process.poll() is not None and not self._stopping:
                stderr = self._process.stderr.read().decode(errors="replace")
                raise RuntimeError(
                    f"pw-record exited with code {self._process.returncode}: "
                    f"{stderr.strip() or '(no error output)'}"
                )
            return np.zeros(0, dtype=np.float32)
        data = b"".join(parts)
        # An odd byte can only come from a pipe read splitting a sample.
        usable = len(data) // 2 * 2
        if usable != len(data):
            self._chunks.queue.appendleft(data[usable:])
        return np.frombuffer(data[:usable], dtype=np.int16).astype(np.float32) / 32768.0

    def stop(self) -> None:
        self._stopping = True
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._thread.join(timeout=2)
            self._process.stdout.close()
            self._process.stderr.close()
            self._process = None


# ---- Windows ---------------------------------------------------------------------------------

def _windows_outputs() -> list[tuple[str, str]]:
    try:
        import soundcard
        return sorted(((sp.id, sp.name) for sp in soundcard.all_speakers()), key=lambda s: s[1].lower())
    except Exception:                       # no audio service, no devices: nothing to list
        return []


def _com_init() -> None:
    """Each thread that talks to WASAPI joins COM's multithreaded apartment first."""
    try:
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)
    except (AttributeError, OSError):
        pass


class WasapiCapture:
    """Stream an output's audio as mono float32 at *rate*: WASAPI loopback of that output (or of
    the default one), read in 20 ms blocks by a thread. The same interface as PipeWireCapture."""

    BLOCK = 0.02

    def __init__(self, target: str | None = None, rate: int = 48000):
        self._target = target
        self.rate = rate
        self._chunks: queue.Queue[np.ndarray] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._error = ""
        self._mic = None

    def start(self) -> None:
        try:
            import soundcard
        except Exception as e:              # missing from a broken install
            raise RuntimeError(f"Windows audio isn't available: {e}") from None
        try:
            speaker = soundcard.get_speaker(self._target) if self._target else soundcard.default_speaker()
            self._mic = soundcard.get_microphone(speaker.id, include_loopback=True)
        except Exception as e:
            raise RuntimeError("No sound output to listen to: plug in or enable speakers or headphones "
                               f"(Windows' Sound settings). ({e})") from None
        self._stopping = False
        self._error = ""
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="wasapi-capture")
        self._thread.start()

    def _read_loop(self) -> None:
        _com_init()
        frames = max(1, int(self.rate * self.BLOCK))
        try:
            with self._mic.recorder(samplerate=self.rate, channels=1, blocksize=frames) as rec:
                while not self._stopping:
                    data = rec.record(numframes=frames)
                    if len(data):
                        self._chunks.put(np.asarray(data, dtype=np.float32).reshape(len(data), -1).mean(axis=1))
        except Exception as e:
            if not self._stopping:
                self._error = f"the Windows audio capture stopped: {e}"

    def read(self) -> np.ndarray:
        """All audio captured since the last call (may be empty). Raises RuntimeError if capture stopped."""
        parts = []
        while True:
            try:
                parts.append(self._chunks.get_nowait())
            except queue.Empty:
                break
        if not parts:
            if self._error:
                raise RuntimeError(self._error)
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts).astype(np.float32, copy=False)

    def stop(self) -> None:
        self._stopping = True
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


AudioCapture = WasapiCapture if system.WINDOWS else PipeWireCapture
