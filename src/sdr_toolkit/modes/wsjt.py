"""FT8 and FT4, decoded by WSJT-X's jt9.

Transmissions start on UTC slot boundaries (every 15 s for FT8, 7.5 s for
FT4). Each slot's audio is written to a WAV file named by the slot's UTC time
and given to jt9. A WebSDR's audio arrives a second or more late, so the slot
is cut from the recording shifted by the "audio delay"; jt9 reports each
signal's time offset (DT), and the delay can follow the median DT.
"""

import logging
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .base import Event, MissingParts, Mode, Setting, Tuning

log = logging.getLogger(__name__)

DECODE = re.compile(r"^(\d{4,6})\s+(-?\d+)\s+(-?[\d.]+)\s+(\d+)\s+(\S)\s+(.*?)\s*$")


class WSJTMode(Mode):
    group = "Digital (weak signal)"
    rate = 12000
    output = "table"
    columns = (("time", "UTC"), ("snr", "dB"), ("dt", "DT"), ("freq", "Audio Hz"), ("message", "Message"))
    requires = ("wsjtx",)
    period = 15.0
    flag = "-8"

    @classmethod
    def common_settings(cls):
        return (
            Setting("delay", "Audio delay", 0.0, "float", -3.0, 10.0, 0.1, 1, suffix=" s",
                    help="How late the audio reaches this PC (a WebSDR stream is often 0.5–3 s "
                         "behind). DT near 0 on decodes means it's right."),
            Setting("auto_delay", "Follow DT", True, "bool",
                    help="Adjust the delay after each slot so the median DT stays near 0."),
            Setting("depth", "Decode depth", 3, "choice",
                    choices=((1, "1 fast"), (2, "2 normal"), (3, "3 deep (most decodes)"))),
            Setting("low", "Lowest audio", 200, "int", 0, 5000, 50, suffix=" Hz"),
            Setting("high", "Highest audio", 3000, "int", 100, 5000, 50, suffix=" Hz",
                    help="The audio range searched. Most activity is between 200 and 3000 Hz."),
            Setting("cq_only", "Only CQ calls", False, "bool"),
            Setting("highlight", "Highlight text", "", "text",
                    help="Rows containing this (e.g. your callsign) are marked."),
        )

    def start(self, values):
        super().start(values)
        if not shutil.which("jt9"):
            raise MissingParts(["wsjtx"])
        self._dir = Path(tempfile.mkdtemp(prefix=f"sdr-toolkit-{self.id}-"))
        self._buf = np.zeros(0, np.float32)
        self._t0 = None          # wall time of self._buf[0]
        self._next_slot = None
        self._results: queue.Queue = queue.Queue()
        self._busy = 0

    def feed(self, audio, now):
        n = len(audio)
        if not n:
            return []
        start = now - n / self.rate
        if self._t0 is None or abs(self._t0 + len(self._buf) / self.rate - start) > 0.25:
            # First audio, or a gap / clock jump: start the buffer again here.
            self._buf = np.zeros(0, np.float32)
            self._t0 = start
        self._buf = np.concatenate([self._buf, audio.astype(np.float32)])
        delay = float(self.values["delay"])
        if self._next_slot is None:
            self._next_slot = (np.floor((self._t0 - delay) / self.period) + 1) * self.period
        events = []
        while True:
            a = self._next_slot + delay
            b = a + self.period
            if self._t0 + len(self._buf) / self.rate < b:
                break
            i = int(round((a - self._t0) * self.rate))
            j = i + int(self.period * self.rate)
            if i >= 0:
                self._decode(self._next_slot, self._buf[i:j])
                events.append(Event("status", f"decoding the {time.strftime('%H:%M:%S', time.gmtime(self._next_slot))} UTC slot"))
            self._next_slot += self.period
        # Keep one slot's worth plus the largest delay.
        keep = int((self.period + 12) * self.rate)
        if len(self._buf) > keep:
            cut = len(self._buf) - keep
            self._buf = self._buf[cut:]
            self._t0 += cut / self.rate
        return events

    def _decode(self, slot, audio):
        name = time.strftime("%y%m%d_%H%M%S", time.gmtime(slot))
        path = self._dir / f"{name}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            peak = max(float(np.max(np.abs(audio))), 1e-6)
            gain = min(1.0, 0.5 / peak) if peak > 0.5 else 1.0
            w.writeframes((np.clip(audio * gain, -1, 1) * 32767).astype("<i2").tobytes())
        v = self.values
        cmd = ["jt9", self.flag, "-p", str(int(self.period)), "-d", str(v["depth"]),
               "-L", str(v["low"]), "-H", str(v["high"]), "-a", str(self._dir), "-t", str(self._dir),
               str(path)]
        self._busy += 1
        threading.Thread(target=self._run, args=(cmd, path, slot), daemon=True).start()

    def _run(self, cmd, path, slot):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=self.period * 2,
                                 cwd=self._dir).stdout
        except (subprocess.TimeoutExpired, OSError) as e:
            out = ""
            log.warning("jt9 failed: %s", e)
        finally:
            path.unlink(missing_ok=True)
        self._results.put((slot, out))

    def poll(self):
        events = []
        while True:
            try:
                slot, out = self._results.get_nowait()
            except queue.Empty:
                break
            self._busy -= 1
            dts = []
            count = 0
            hl = self.values["highlight"].strip().upper()
            for line in out.splitlines():
                m = DECODE.match(line)
                if not m:
                    continue
                msg = m.group(6)
                dts.append(float(m.group(3)))
                if self.values["cq_only"] and not msg.startswith("CQ"):
                    continue
                count += 1
                row = {"time": time.strftime("%H:%M:%S", time.gmtime(slot)), "snr": int(m.group(2)),
                       "dt": float(m.group(3)), "freq": int(m.group(4)), "message": msg}
                if hl and hl in msg.upper():
                    row["_style"] = "highlight"
                elif msg.startswith("CQ"):
                    row["_style"] = "cq"
                events.append(Event("row", data=row))
            note = f"{time.strftime('%H:%M:%S', time.gmtime(slot))} UTC: {count} decoded"
            if dts:
                med = float(np.median(dts))
                note += f", median DT {med:+.1f} s"
                if self.values["auto_delay"] and abs(med) > 0.2:
                    new = float(np.clip(self.values["delay"] + 0.7 * med, -3.0, 10.0))
                    self.values["delay"] = round(new, 1)
                    note += f", delay now {self.values['delay']:.1f} s"
                    events.append(Event("setting", data={"delay": self.values["delay"]}))
            events.append(Event("status", note))
        return events

    def stop(self):
        events = self.poll()
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)
        return events

    def markers(self):
        return [(self.values["low"], "low"), (self.values["high"], "high")]


_TUNING_NOTES = ("Set the SDR's dial to the frequency below in USB; the signals then appear "
                 "between about 200 and 3000 Hz in the audio. Your PC clock must be right "
                 "(it syncs automatically on Ubuntu); a WebSDR's stream delay is handled by "
                 "Audio delay / Follow DT.")


class FT8Mode(WSJTMode):
    id = "ft8"
    name = "FT8"
    summary = "15-second weak-signal exchanges, the busiest digital mode on HF. Decoded by WSJT-X's jt9."
    period = 15.0
    flag = "-8"
    settings = WSJTMode.common_settings()
    tuning = Tuning(
        sdr_mode="USB",
        bandwidth="3 kHz (or wider: up to 4 kHz if the SDR allows)",
        frequencies=(("1.840 MHz", "160 m"), ("3.573 MHz", "80 m"), ("5.357 MHz", "60 m"),
                     ("7.074 MHz", "40 m"), ("10.136 MHz", "30 m"), ("14.074 MHz", "20 m, the busiest"),
                     ("18.100 MHz", "17 m"), ("21.074 MHz", "15 m"), ("24.915 MHz", "12 m"),
                     ("28.074 MHz", "10 m"), ("50.313 MHz", "6 m"), ("144.174 MHz", "2 m")),
        notes=_TUNING_NOTES,
    )


class FT4Mode(WSJTMode):
    id = "ft4"
    name = "FT4"
    summary = "7.5-second contest-style exchanges. Decoded by WSJT-X's jt9."
    period = 7.5
    flag = "-5"
    settings = WSJTMode.common_settings()
    tuning = Tuning(
        sdr_mode="USB",
        bandwidth="3 kHz",
        frequencies=(("3.575 MHz", "80 m"), ("7.0475 MHz", "40 m"), ("10.140 MHz", "30 m"),
                     ("14.080 MHz", "20 m"), ("18.104 MHz", "17 m"), ("21.140 MHz", "15 m"),
                     ("24.919 MHz", "12 m"), ("28.180 MHz", "10 m"), ("50.318 MHz", "6 m")),
        notes=_TUNING_NOTES,
    )
