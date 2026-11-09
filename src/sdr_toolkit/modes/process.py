"""Modes that run an outside decoder and turn its output lines into events.

The decoder's input is one of:
  - the window's audio, written to its stdin (``takes_audio``, the default);
  - nothing: it reads the radio or a recording itself;
  - another program's output (``upstream()``, e.g. rtl_fm | direwolf);
  - bytes made in Python (``feeder()``, e.g. FM audio demodulated from a recording).
"""

import logging
import queue
import shutil
import subprocess
import threading

import numpy as np

from .base import Event, Mode

log = logging.getLogger(__name__)
UPSTREAM = "\x00upstream "      # marks an error line from the upstream program


class ProcessMode(Mode):
    takes_audio = True

    def command(self) -> list[str]:
        raise NotImplementedError

    def upstream(self) -> list[str] | None:
        """A program whose output is the decoder's input, if any."""
        return None

    def feeder(self):
        """An iterator of bytes for the decoder's input, if any (runs in a thread)."""
        return None

    def parse(self, line: str) -> list[Event]:
        return [Event("line", line)]

    def workdir(self):
        return None

    def env(self):
        """Environment for the decoder (None: this process's)."""
        return None

    def start(self, values):
        super().start(values)
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._up = None
        self._feed_thread = None
        cmd = self.command()
        up = self.upstream()
        for prog in [cmd[0]] + ([up[0]] if up else []):
            if not shutil.which(prog):
                pkg = self.requires[0] if self.requires else prog
                raise RuntimeError(f"{prog} is not installed. Install it with: sudo apt install {pkg}")
        self._name = cmd[0]
        feeder = self.feeder()
        stdin = subprocess.PIPE if (self.takes_audio or feeder is not None) else subprocess.DEVNULL
        if up:
            log.info("starting %s", " ".join(up))
            self._up = subprocess.Popen(up, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            stdin = self._up.stdout
            threading.Thread(target=self._read_stderr, args=(self._up, up[0]), daemon=True).start()
        log.info("starting %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      cwd=self.workdir(), env=self.env(), bufsize=0)
        if self._up:
            self._up.stdout.close()          # the decoder holds the pipe now
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        if feeder is not None:
            self._feed_thread = threading.Thread(target=self._feed_from, args=(feeder,), daemon=True)
            self._feed_thread.start()

    def _read(self):
        for raw in self._proc.stdout:
            self._lines.put(raw.decode(errors="replace").rstrip("\r\n"))
        self._lines.put(None)

    def _read_stderr(self, proc, name):
        for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.info("%s: %s", name, line)
                if any(w in line.lower() for w in ("error", "no supported", "failed", "fatal")):
                    self._lines.put(UPSTREAM + f"{name}: {line}")

    def _feed_from(self, feeder):
        try:
            for chunk in feeder:
                if self._proc is None or self._proc.poll() is not None:
                    break
                self._proc.stdin.write(chunk)
        except (BrokenPipeError, OSError, ValueError):
            pass
        except Exception:  # a broken recording mustn't kill the window; show it in the log
            log.exception("feeding %s", self._name)
            self._lines.put("Could not read the recording (see the app log)")
        finally:
            try:
                self._proc.stdin.close()
            except (OSError, AttributeError):
                pass

    def feed(self, audio, now):
        if not self.takes_audio or self._proc.poll() is not None:
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
                events += self.finished(self._proc.poll())
                continue
            if line.startswith(UPSTREAM):
                events += self.upstream_error(line[len(UPSTREAM):])
                continue
            events += self.parse(line)
        return events

    def upstream_error(self, line) -> list[Event]:
        return [Event("line", line, style="warn"), Event("status", line)]

    def finished(self, code) -> list[Event]:
        """The decoder exited by itself (a recording ended, or an error)."""
        return [Event("status", f"{self._name} stopped (exit code {code})"),
                Event("line", f"{self._name} stopped (exit code {code})", style="warn" if code else "dim")]

    def stop(self):
        proc = getattr(self, "_proc", None)
        if proc is None:
            return []
        for p in (self._up, proc):
            if p is None:
                continue
            if p is proc and self.takes_audio:
                try:
                    p.stdin.close()      # let it finish what it has
                except OSError:
                    pass
            else:
                p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
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
