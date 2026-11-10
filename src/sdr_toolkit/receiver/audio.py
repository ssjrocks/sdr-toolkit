"""The receiver's sound: 48 kHz mono to the default output through PipeWire's pw-play."""

import collections
import logging
import subprocess
import threading

import numpy as np

log = logging.getLogger(__name__)
RATE = 48000
MAX_QUEUED = 0.5             # s; beyond this the oldest audio is dropped so it doesn't lag


class AudioOut:
    def __init__(self, command=None):
        self.command = command or ["pw-play", "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16",
                                   "--latency", "50ms", "-"]
        self.error = ""
        self._queue: collections.deque = collections.deque()
        self._queued = 0
        self._ready = threading.Condition()
        self._halt = False
        try:
            self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE)
        except FileNotFoundError:
            self.proc = None
            self.error = "pw-play isn't installed (pipewire-bin): no sound"
            return
        self._thread = threading.Thread(target=self._write, daemon=True, name="audio-out")
        self._thread.start()

    def play(self, audio: np.ndarray, volume=1.0):
        if self.proc is None or not len(audio):
            return
        pcm = (np.clip(audio * volume, -1, 1) * 32767).astype("<i2").tobytes()
        with self._ready:
            self._queue.append(pcm)
            self._queued += len(pcm)
            while self._queued > RATE * 2 * MAX_QUEUED and len(self._queue) > 1:
                self._queued -= len(self._queue.popleft())
            self._ready.notify()

    def _write(self):
        while True:
            with self._ready:
                while not self._queue and not self._halt:
                    self._ready.wait(0.5)
                if self._halt:
                    break
                pcm = self._queue.popleft()
                self._queued -= len(pcm)
            try:
                self.proc.stdin.write(pcm)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                err = self.proc.stderr.read().decode(errors="replace").strip() if self.proc.stderr else ""
                self.error = f"the sound output stopped{': ' + err if err else ''}"
                log.warning(self.error)
                break

    def close(self):
        if self.proc is None:
            return
        with self._ready:
            self._halt = True
            self._ready.notify()
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
