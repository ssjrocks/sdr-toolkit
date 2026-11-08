"""Modes that run an outside decoder: audio goes to its stdin, text comes back."""

import logging
import queue
import shutil
import subprocess
import threading

import numpy as np

from .base import Event, Mode

log = logging.getLogger(__name__)


class ProcessMode(Mode):
    """Subclasses give ``command()`` and turn output lines into events in ``parse()``."""

    def command(self) -> list[str]:
        raise NotImplementedError

    def parse(self, line: str) -> list[Event]:
        return [Event("line", line)]

    def start(self, values):
        super().start(values)
        self._lines: queue.Queue[str | None] = queue.Queue()
        cmd = self.command()
        if not shutil.which(cmd[0]):
            raise RuntimeError(f"{cmd[0]} is not installed. Install it with: sudo apt install {self.requires[0]}")
        log.info("starting %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=self.workdir(), bufsize=0,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def workdir(self):
        return None

    def _read(self):
        for raw in self._proc.stdout:
            self._lines.put(raw.decode(errors="replace").rstrip("\r\n"))
        self._lines.put(None)

    def feed(self, audio, now):
        if self._proc.poll() is not None:
            return []
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        try:
            self._proc.stdin.write(pcm)
        except (BrokenPipeError, OSError):
            pass
        return []

    def poll(self):
        events = []
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                code = self._proc.poll()
                events.append(Event("status", f"{self.command()[0]} stopped (exit code {code})"))
                events.append(Event("line", f"{self.command()[0]} stopped unexpectedly (exit code {code})",
                                    style="warn"))
                continue
            events += self.parse(line)
        return events

    def stop(self):
        proc = getattr(self, "_proc", None)
        if proc is None:
            return []
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._reader.join(timeout=2)
        events = []
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                events += self.parse(line)
        self._proc = None
        return events
