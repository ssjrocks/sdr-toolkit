"""AIS ship positions on 161.975 / 162.025 MHz, decoded by direwolf."""

import shutil
import tempfile
import time
from pathlib import Path

from .base import Event, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode, fm_audio_chunks

AUDIO_RATE = 48000


class AISMode(RadioMode):
    id = "ais"
    name = "AIS ships (VHF)"
    group = "RTL-SDR"
    summary = "Ship identities, positions, speed and course from their AIS transponders. Decoded by direwolf."
    requires = ("direwolf", "rtl-sdr")
    iq_rate = 240000
    settings = RADIO_SETTINGS + (
        Setting("channel", "Channel", "161.975M", "choice", restart=True,
                choices=(("161.975M", "AIS 1 (87B), 161.975 MHz"), ("162.025M", "AIS 2 (88B), 162.025 MHz"),
                         ("custom", "Other (below)")),
                help="Ships alternate between both channels: either one shows them all, more slowly."),
        Setting("custom_freq", "Other frequency", "161.975M", "text", restart=True),
        Setting("file_rate", "Recording sample rate", 240000, "int", 48000, 3200000, 1000, suffix=" Hz",
                restart=True),
        Setting("file_offset", "Channel offset in recording", 0, "int", -1600000, 1600000, 1000, suffix=" Hz",
                restart=True, help="How far the AIS channel sits from the recording's centre frequency."),
        Setting("raw", "Show NMEA sentences", False, "bool"),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR, FM-demodulated by rtl_fm (the toolkit tunes it)",
        bandwidth="25 kHz channel",
        frequencies=(("161.975 MHz", "AIS 1"), ("162.025 MHz", "AIS 2")),
        notes="Ships are heard to about 30-60 km from a high antenna near water; inland you may only "
              "hear ships on rivers. A VHF marine or 2 m antenna works well.",
    )

    def freq(self):
        return self.values["custom_freq"] if self.values["channel"] == "custom" else self.values["channel"]

    def upstream(self):
        if not self.live():
            return None
        v = self.values
        cmd = ["rtl_fm", "-d", self.device_index().lstrip(":"), "-f", self.freq(), "-M", "fm",
               "-s", str(AUDIO_RATE), "-p", str(v["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def feeder(self):
        if self.live():
            return None
        return fm_audio_chunks(self.file_path(), self.values["file_rate"], self.values["file_offset"],
                               AUDIO_RATE, 25000)

    def command(self):
        self._dir = Path(tempfile.mkdtemp(prefix="sdr-toolkit-ais-"))
        (self._dir / "direwolf.conf").write_text(
            f"ADEVICE stdin null\nARATE {AUDIO_RATE}\nACHANNELS 1\nMODEM AIS\nAGWPORT 0\nKISSPORT 0\n")
        self._ready = False
        return ["direwolf", "-c", "direwolf.conf", "-r", str(AUDIO_RATE), "-n", "1", "-b", "16", "-t", "0",
                "-B", "AIS", "-"]

    def workdir(self):
        return str(self._dir)

    def parse(self, line):
        line = line.strip()
        if not self._ready:
            if line.startswith("Disabled AGW") or line.startswith("Disabled KISS"):
                self._ready = True
                return [Event("status", "listening for AIS")]
            return [Event("line", line, style="warn")] if "rror" in line else []
        if not line or "audio level" in line or "input level" in line or line.startswith(("Solution", "Setting audio")):
            return []
        if line.startswith("["):
            if not self.values["raw"]:
                return []
            nmea = line[line.find("!AIVDM"):] if "!AIVDM" in line else line
            return [Event("line", f"          {nmea}", style="dim")]
        if line.startswith("AIS "):
            return [Event("line", f"{time.strftime('%H:%M:%S')}  {line}")]
        return [Event("line", f"          {line}")]

    def stop(self):
        events = super().stop()
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)
        return events

    def finished(self, code):
        if not self.live():
            return [Event("status", "recording finished")]
        return super().finished(code)
