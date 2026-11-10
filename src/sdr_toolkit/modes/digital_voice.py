"""Digital voice: DMR, P25, NXDN, D-Star, YSF, dPMR..., decoded by DSD-FME (with mbelib).

DSD-FME reads the RTL-SDR itself, or a 48 kHz FM-demodulated WAV; an IQ recording is
demodulated to such a WAV first. Calls appear in its event log, which is shown as it grows.
Voice plays on the speakers (or not). Encrypted calls stay encrypted: the toolkit doesn't
offer DSD-FME's key options.
"""

import shutil
import tempfile
import time
import wave
from pathlib import Path

from .base import Event, MissingParts, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode, fm_audio_chunks

DSD_FME = Path.home() / ".local/share/sdr-toolkit/dsd-fme/bin/dsd-fme"
PROTOCOLS = (
    ("-fa", "Automatic (P25 phase 1, DMR, D-Star, YSF, X2-TDMA)"),
    ("-fs", "DMR (simplex and repeater)"),
    ("-f1", "P25 phase 1"),
    ("-f2", "P25 phase 2"),
    ("-fn", "NXDN96 (12.5 kHz)"),
    ("-fi", "NXDN48 (6.25 kHz)"),
    ("-fd", "D-Star"),
    ("-fy", "Yaesu System Fusion"),
    ("-fm", "dPMR"),
    ("-fz", "M17"),
    ("-fp", "ProVoice"),
    ("-fh", "EDACS"),
)
BORING = ("█", "╗", "╝", "═", "Build Version", "MBElib Version", "CODEC2", "Writing + Appending",
          "Audio In Device", "Audio Out Device", "Setting RTL", "Dev 0 Freq")


def dsd_fme() -> str | None:
    if DSD_FME.is_file():
        return str(DSD_FME)
    return shutil.which("dsd-fme")


class DigitalVoiceMode(RadioMode):
    id = "dmr"
    name = "DMR / P25 / digital voice"
    group = "RTL-SDR"
    summary = ("Digital voice radio: DMR, P25, NXDN, D-Star, Yaesu Fusion, dPMR, M17. Shows calls (talkgroup, "
               "radio IDs, callsigns) and plays the voice. Decoded by DSD-FME.")
    requires = ("dsd-fme",)
    iq_rate = 240000
    settings = RADIO_SETTINGS + (
        Setting("freq_mhz", "Frequency", "", "text", restart=True,
                help="MHz, e.g. 438.875 for a DMR repeater's output, or a P25 voice channel."),
        Setting("protocol", "Decode", "-fa", "choice", restart=True, choices=PROTOCOLS,
                help="Automatic covers the common ones; pick one if you know it (needed for NXDN, "
                     "dPMR, M17, EDACS)."),
        Setting("bw", "Receiver bandwidth", 12, "choice", restart=True,
                choices=((6, "6 kHz (NXDN48, dPMR)"), (8, "8 kHz"), (12, "12 kHz (DMR, P25, NXDN96)"),
                         (16, "16 kHz"), (24, "24 kHz (wide FM)")),
                help="RTL-SDR input: the FM channel filter."),
        Setting("squelch", "Squelch", 0, "int", 0, 100, restart=True,
                help="RTL-SDR input: 0 is off. DSD-FME's RMS level; raise it to skip noise between calls."),
        Setting("voice", "Play voice", True, "bool", restart=True,
                help="On your speakers (PulseAudio). Encrypted calls are muted."),
        Setting("save_calls", "Save each call as a WAV", False, "bool", restart=True),
        Setting("folder", "Save calls to", str(Path.home() / "Music" / "SDR Toolkit calls"), "text", restart=True),
        Setting("details", "Show decoder details", False, "bool",
                help="DSD-FME's frame-by-frame output (sync, slot, errors)."),
        Setting("file_rate", "Recording sample rate", 240000, "int", 48000, 3200000, 1000, suffix=" Hz",
                restart=True, help="For .cu8 recordings. A .wav recording must be 48 kHz mono FM audio."),
        Setting("file_offset", "Channel offset in recording", 0, "int", -1600000, 1600000, 1000,
                suffix=" Hz", restart=True),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR; DSD-FME tunes it and FM-demodulates (no WebSDR: their FM audio "
                 "filtering usually destroys these signals)",
        bandwidth="12.5 kHz channels (6.25 kHz for NXDN48 and dPMR)",
        frequencies=(("438–440 MHz, 144–148 MHz", "Amateur DMR, D-Star, YSF and M17 repeaters (check local lists)"),
                     ("400–520 MHz", "Business and government DMR, P25 and NXDN"),
                     ("380–400, 800–870 MHz", "Public-safety P25 in some countries (often encrypted)")),
        notes="Set the frequency exactly and the ppm correction for your dongle: digital voice needs the "
              "signal centred. Encrypted calls show as encrypted. Listening to some services is "
              "restricted by law in some countries.",
    )

    def command(self):
        exe = dsd_fme()
        if exe is None:
            raise MissingParts(["dsd-fme"])
        v = self.values
        self._dir = Path(tempfile.mkdtemp(prefix="sdr-toolkit-dmr-"))
        self._events = self._dir / "events.log"
        self._seen = 0
        self._calls = 0
        if self.live():
            try:
                freq = float(v["freq_mhz"])
            except ValueError:
                raise RuntimeError("Set the frequency in Settings (MHz).") from None
            gain = self.gain_db() or "0"           # 0 = auto in DSD-FME
            dev = self.device_index().lstrip(":")
            b = self.bridge()
            if b:
                src = f"rtltcp:127.0.0.1:{b.local_port}:{freq}M:{gain}:{v['ppm']}:{v['bw']}:{v['squelch']}:2"
            else:
                src = f"rtl:{dev}:{freq}M:{gain}:{v['ppm']}:{v['bw']}:{v['squelch']}:2"
        else:
            src = self._wav_from_recording()
        cmd = [exe, v["protocol"], "-i", src, "-o", "pulse" if v["voice"] else "null", "-J", str(self._events)]
        if v["save_calls"]:
            folder = Path(v["folder"]).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            cmd += ["-P", "-7", str(folder)]
        return cmd

    def _wav_from_recording(self) -> str:
        path = self.file_path()
        if path.lower().endswith(".wav"):
            return path
        out = self._dir / "audio.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            for chunk in fm_audio_chunks(path, self.values["file_rate"], self.values["file_offset"],
                                         48000, 12500, level=0.5):
                w.writeframes(chunk)
        return str(out)

    def parse(self, line):
        problem = self.device_problem(line)
        if problem is not None:
            return problem
        if self._problem or not line.strip() or any(b in line for b in BORING):
            return []
        if "Failed to open" in line:
            return []
        if self.values["details"]:
            return [Event("line", f"          {line.rstrip()}", style="dim")]
        return []

    def poll(self):
        return super().poll() + self._read_events()

    def _read_events(self):
        try:
            lines = self._events.read_text(errors="replace").splitlines()
        except OSError:
            return []
        new, self._seen = lines[self._seen:], len(lines)
        events = []
        for line in new:
            if "Started and Event History Initialized" in line or "Any decoded voice calls" in line:
                continue
            text = line.rstrip("; ")
            if text:
                self._calls += 1
                events.append(Event("line", text))
        if events:
            events.append(Event("status", f"last activity {time.strftime('%H:%M:%S')}"))
        return events

    def listening_on(self):
        return f"{self.values['freq_mhz']} MHz"

    def activity(self):
        return f"{super().activity()} · {self._calls} call events"

    def stop(self):
        events = super().stop() + self._read_events()
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)
        return events

    def finished(self, code):
        if not self.live() and code == 0:
            return self._read_events() + [Event("status", "recording finished")]
        return super().finished(code)
