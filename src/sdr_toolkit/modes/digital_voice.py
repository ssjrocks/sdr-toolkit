"""Digital voice: DMR, P25, NXDN, D-Star, YSF, dPMR..., decoded by DSD-FME (with mbelib).

DSD-FME reads the RTL-SDR itself, or a 48 kHz FM-demodulated WAV; an IQ recording is
demodulated to such a WAV first. Calls appear in its event log, which is shown as it grows.
Before a call is complete, its frame sync lines show that a signal is being heard: when one
starts (protocol, slot, colour code), every 10 s while it lasts, and a summary when it ends.
Voice plays on the speakers (or not). Encrypted calls stay encrypted: the toolkit doesn't
offer DSD-FME's key options.
"""

import collections
import json
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from ..receiver import dsd
from .base import Event, MissingParts, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode, fm_audio_chunks
from .. import system

DSD_FME = system.data_dir() / "dsd-fme" / "bin" / "dsd-fme"
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


ANSI = re.compile(r"\x1b\[[0-9;]*m")
COLOUR_CODE = re.compile(r"Color Code=(\d+)")
SLOT = re.compile(r"\[SLOT([12])\]")
HEARD_AFTER = 6          # frame syncs within QUIET of each other that count without a verified one
QUIET = 1.0              # s without a frame sync: the transmission has ended
PROGRESS = 10.0          # s between "still hearing" lines


def dsd_fme() -> str | None:
    if DSD_FME.is_file():
        return str(DSD_FME)
    return shutil.which("dsd-fme")


def stream_node(pid) -> int | None:
    """PipeWire's id for the audio output stream of process pid (None if it has none yet)."""
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
        objects = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for o in objects:
        props = (o.get("info") or {}).get("props") or {}
        if (o.get("type") == "PipeWire:Interface:Node" and props.get("media.class") == "Stream/Output/Audio"
                and str(props.get("application.process.id")) == str(pid)):
            return o["id"]
    return None


RECEIVER_AUDIO = "receiver"      # input: FM audio from Spectrum & receiver


class DigitalVoiceMode(RadioMode):
    id = "dmr"
    name = "DMR / P25 / digital voice (one frequency)"
    hidden = True                 # in the list as the explorer; this decodes for it and for decode points
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
        Setting("voice_volume", "Voice volume", 100, "int", 0, 150, 5, suffix=" %",
                help="The decoded voice's loudness (its own stream in the sound settings, \"DSD-FME\")."),
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
        self._tx = None                  # the transmission being heard (frame syncs)
        if v["input"] == RECEIVER_AUDIO:
            # FM audio from Spectrum & receiver, sent to DSD-FME over TCP (it connects as SDR++'s
            # audio sink's client would).
            self.rate = 48000
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            srv.settimeout(15)
            self._server, self._conn = srv, None

            def accept():
                try:
                    self._conn, _ = srv.accept()
                except OSError:
                    pass

            threading.Thread(target=accept, daemon=True, name="dsd-audio").start()
            src = f"tcp:127.0.0.1:{srv.getsockname()[1]}"
        elif self.live():
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

    def feed(self, audio, now):
        """Receiver audio (48 kHz FM) for DSD-FME, when it's fed from Spectrum & receiver."""
        conn = getattr(self, "_conn", None)
        if conn is not None and len(audio):
            try:
                conn.sendall((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
            except OSError:
                self._conn = None
        return []

    def start(self, values):
        super().start(values)
        if int(self.values.get("voice_volume", 100)) != 100:
            self.set_voice_volume(self.values["voice_volume"])

    def apply(self, values):
        old = self.values.get("voice_volume")
        super().apply(values)
        if self.values.get("voice_volume") != old:
            self.set_voice_volume(self.values["voice_volume"])

    def set_voice_volume(self, percent):
        """The decoded voice's volume: DSD-FME's own stream in PipeWire, set with wpctl (DSD-FME
        can't change its gain while running). Done in a thread; the stream may not be there yet."""
        self.values["voice_volume"] = int(percent)
        proc = getattr(self, "_proc", None)
        if proc is None or not self.values.get("voice") or not shutil.which("wpctl"):
            return
        self._volume_seq = getattr(self, "_volume_seq", 0) + 1
        seq = self._volume_seq

        def run():
            for _ in range(20):
                if seq != self._volume_seq or proc.poll() is not None:
                    return
                node = stream_node(proc.pid)
                if node is not None:
                    subprocess.run(["wpctl", "set-volume", str(node), f"{int(percent) / 100:.2f}"],
                                   capture_output=True, timeout=5)
                    return
                time.sleep(0.5)

        threading.Thread(target=run, daemon=True, name="dsd-volume").start()

    def _stop_dsd(self):
        # DSD-FME ends on SIGINT (Ctrl+C) and ignores SIGTERM for seconds. Reading the receiver's
        # audio it only sees the signal once its read returns, so that connection is closed first.
        for attr in ("_conn", "_server"):
            sock = getattr(self, attr, None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, attr, None)
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            if system.WINDOWS:
                proc.terminate()                # no SIGINT to send on Windows
            else:
                proc.send_signal(signal.SIGINT)
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()

    SHORT = {"voice": "Calls: the talkgroup and radio ID show when one starts.",
             "control": "A trunked system's control channel: the calls are on other frequencies.",
             "idle": "An idle repeater's beacon: calls come when someone talks.",
             "data": "Data (GPS positions, messages, telemetry), not voice."}

    def _opening(self, tx) -> str:
        chan = tx["chan"]
        k = chan.kinds.most_common(1)[0][0] if chan.kinds else ""
        return f"{time.strftime('%H:%M:%S')}  {chan.line()}." + (f" {self.SHORT[k]}" if k in self.SHORT else "")

    def _heard(self, line, now, parsed=None) -> list[Event]:
        """A frame sync line ("Sync: +DMR  [SLOT1]  slot2  | Color Code=12 | VC1")."""
        rest = line.split("Sync:", 1)[1].strip().lstrip("+-").split()
        if not rest:
            return []
        proto = rest[0]
        tx = self._tx
        if tx is None or now - tx["last"] > QUIET or tx["proto"] != proto:
            events = self._ended(now) if tx is not None else []
            tx = self._tx = {"proto": proto, "start": now, "last": now, "frames": 0, "slots": collections.Counter(),
                             "cc": collections.Counter(), "kinds": collections.Counter(), "errors": 0,
                             "shown": False, "reported": now, "chan": dsd.Channel(None)}
        else:
            events = []
        tx["last"] = now
        tx["frames"] += 1
        if parsed is not None:
            tx["chan"].add(parsed)
        slot = SLOT.search(line)
        if slot:
            tx["slots"][slot.group(1)] += 1
        cc = COLOUR_CODE.search(line)              # decoded and passed its FEC: a verified frame
        if cc:
            tx["cc"][int(cc.group(1))] += 1
        kind = line.rsplit("|", 1)[-1].strip().split()[0] if cc and "|" in line else ""
        if kind:
            tx["kinds"]["voice" if kind.startswith(("VC", "VLC", "TLC")) else
                        "idle" if kind.startswith("IDLE") else "data"] += 1
        if "Err" in line or "ERR" in line:
            tx["errors"] += 1
        if not tx["shown"] and (cc or tx["frames"] >= HEARD_AFTER):
            tx["shown"] = True
            tx["reported"] = now
            events += [Event("line", self._opening(tx)),
                       Event("status", f"hearing {tx['chan'].name()} {time.strftime('%H:%M:%S')}")]
        elif tx["shown"] and now - tx["reported"] >= PROGRESS:
            tx["reported"] = now
            events.append(Event("line", f"{time.strftime('%H:%M:%S')}  {tx['chan'].what()} still heard: "
                                        f"{self._summary(tx)}", style="dim"))
        return events

    @staticmethod
    def _summary(tx):
        s = f"{tx['last'] - tx['start']:.1f} s, {tx['frames']} frames"
        if tx["slots"]:
            s += " (" + ", ".join(f"slot {k}: {n}" for k, n in sorted(tx["slots"].items())) + ")"
        chan = tx.get("chan")
        if chan is not None and chan.kinds:
            s += ", " + ", ".join(f"{n} {k}" for k, n in chan.kinds.most_common())
            decoded = chan.contents()
            if decoded:
                s += f" (decoded: {decoded})"
            elif chan.kinds["data"]:
                s += " (the data wasn't decoded further: DSD-FME didn't recognise its format)"
        if tx["cc"]:
            s += f", colour code {tx['cc'].most_common(1)[0][0]}"
        if tx["errors"]:
            s += f", {tx['errors']} with errors"
        return s

    def _ended(self, now) -> list[Event]:
        tx, self._tx = self._tx, None
        if tx is None or not tx["shown"]:
            return []
        return [Event("line", f"{time.strftime('%H:%M:%S')}  {tx['chan'].what()} ended: {self._summary(tx)}")]

    def parse(self, line):
        line = ANSI.sub("", line)
        events = []
        parsed = dsd.parse(line)
        if "Sync:" in line:
            events = self._heard(line, time.monotonic(), parsed)
        elif self._tx is not None:
            if "ERR" in line or "Err" in line:
                self._tx["errors"] += 1
            if parsed is not None:
                self._tx["chan"].add(parsed)
        if parsed is not None:
            events.append(Event("dsd", data=parsed))        # for the explorer: frames, calls, control
            if parsed["what"] == "call":
                c = parsed
                events.append(Event("line", f"{time.strftime('%H:%M:%S')}  "
                                            f"{'Group' if c['group'] else 'Private'} call"
                                            f"{' (emergency)' if c['emergency'] else ''} to {c['tgt']} from radio "
                                            f"{c['src']}" + (f", slot {c['slot']}" if c['slot'] else "")
                                            + (": encrypted, no audio" if c["encrypted"] else ""),
                                    style="dim" if self._same_call(c) else ""))
            elif parsed["what"] in ("position", "text", "data", "control"):
                text = {"position": lambda e: f"GPS position: {e['lat']:.5f}, {e['lon']:.5f}",
                        "text": lambda e: f"Text message: {e['text']}",
                        "data": lambda e: f"Data: {e['name']}",
                        "control": lambda e: f"Control: {e.get('name') or e['text']}"}[parsed["what"]](parsed)
                repeat = parsed["what"] in ("data", "control") and self._seen_recently(text)
                events.append(Event("line", f"{time.strftime('%H:%M:%S')}  {text}",
                                    style="dim" if repeat or parsed["what"] in ("data", "control") else ""))
        return events + self._parse(line)

    def _seen_recently(self, text, window=10.0) -> bool:
        now = time.monotonic()
        seen = getattr(self, "_texts_seen", {})
        self._texts_seen = {k: t for k, t in seen.items() if now - t < window}
        repeat = text in self._texts_seen
        self._texts_seen[text] = now
        return repeat

    def _same_call(self, c) -> bool:
        """DSD-FME repeats a call's line every superframe: say it once (until 5 s of quiet)."""
        key = (c["slot"], c["tgt"], c["src"])
        now = time.monotonic()
        seen = getattr(self, "_calls_seen", {})
        self._calls_seen = {k: t for k, t in seen.items() if now - t < 5}
        repeat = key in self._calls_seen
        self._calls_seen[key] = now
        return repeat

    def _parse(self, line):
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
        events = super().poll() + self._read_events()
        if getattr(self, "_tx", None) is not None and time.monotonic() - self._tx["last"] > QUIET:
            events += self._ended(time.monotonic())
        return events

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
        self._stop_dsd()
        events = super().stop() + self._read_events() + self._ended(time.monotonic())
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)
        return events

    def finished(self, code):
        if not self.live() and code == 0 and self.values["input"] != RECEIVER_AUDIO:
            return self._read_events() + self._ended(time.monotonic()) + [Event("status", "recording finished")]
        return super().finished(code)
