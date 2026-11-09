"""APRS and AX.25 packet radio, decoded by direwolf."""

import re
import shutil
import tempfile
import time
from pathlib import Path

from .base import Event, Setting, Tuning
from .process import ProcessMode, looks_like_error

PACKET = re.compile(r"^\[[\d.]+\]\s+(.*)$")


class APRSMode(ProcessMode):
    id = "aprs"
    name = "APRS / packet"
    group = "Packet & paging"
    summary = "AX.25 packet radio: APRS positions, weather, messages and telemetry. Decoded by direwolf."
    rate = 48000
    requires = ("direwolf",)
    settings = (
        Setting("baud", "Speed", "1200", "choice", restart=True,
                choices=(("300", "300 baud (HF, AFSK 1600/1800 Hz)"), ("1200", "1200 baud (VHF APRS, AFSK)"),
                         ("2400", "2400 baud (QPSK)"), ("4800", "4800 baud (8PSK)"),
                         ("9600", "9600 baud (G3RUH, needs flat FM audio)")),
                help="1200 for normal VHF APRS. 9600 rarely survives a WebSDR's audio filtering."),
        Setting("mark", "Mark tone", 1200, "int", 300, 3000, 10, suffix=" Hz", restart=True,
                help="AFSK only. 1200 for 1200 baud; 1600 for 300 baud HF."),
        Setting("space", "Space tone", 2200, "int", 300, 3000, 10, suffix=" Hz", restart=True,
                help="AFSK only. 2200 for 1200 baud; 1800 for 300 baud HF."),
        Setting("profile", "Demodulator", "A+", "choice", restart=True,
                choices=(("A+", "A+ (best for de-emphasised FM audio)"), ("B+", "B+ (flat audio)"),
                         ("A", "A, single"), ("B", "B, single")),
                help="direwolf's demodulator profile. The '+' versions run several decoders and pick "
                     "what passes the checksum."),
        Setting("fix_bits", "Repair bit errors", 1, "choice", restart=True,
                choices=((0, "Off"), (1, "Single bits"), (2, "Up to two bits")),
                help="Try flipping bits to pass the checksum. More finds more packets, and a few wrong ones."),
        Setting("levels", "Show audio levels", False, "bool",
                help="direwolf's audio level for each packet. About 50 is ideal."),
    )
    tuning = Tuning(
        sdr_mode="FM (narrow) on VHF/UHF; USB on HF (300 baud)",
        bandwidth="FM: 12.5–16 kHz. HF: 500 Hz–2.4 kHz around the tones.",
        frequencies=(
            ("144.390 MHz FM", "North America"), ("144.800 MHz FM", "Europe, Africa, Russia"),
            ("145.175 MHz FM", "Australia"), ("144.575 MHz FM", "New Zealand"),
            ("144.640 MHz FM", "China"), ("144.660 MHz FM", "Japan"),
            ("145.825 MHz FM", "ISS digipeater (1200 baud)"),
            ("10.147–10.151 MHz USB", "HF APRS, 300 baud: tune the tones to the Mark/Space settings"),
        ),
        notes="Leave the receiver's squelch open (or off): direwolf needs the start of each packet. "
              "Packets are short bursts; one every few seconds is normal near a city.",
    )

    def command(self):
        self._dir = Path(tempfile.mkdtemp(prefix="sdr-toolkit-aprs-"))
        v = self.values
        baud = int(v["baud"])
        modem = f"MODEM {baud}"
        if baud <= 1200:
            modem += f" {v['mark']}:{v['space']} {v['profile']}"
        (self._dir / "direwolf.conf").write_text(
            "ADEVICE stdin null\n"
            f"ARATE {self.rate}\n"
            "ACHANNELS 1\n"
            f"{modem}\n"
            f"FIX_BITS {v['fix_bits']}\n"
            "AGWPORT 0\n"
            "KISSPORT 0\n"
        )
        self._ready = False
        self._banner = []
        return ["direwolf", "-c", str(self._dir / "direwolf.conf"), "-r", str(self.rate),
                "-n", "1", "-b", "16", "-t", "0", "-"]

    def workdir(self):
        return str(self._dir)

    def parse(self, line):
        if not self._ready:
            # direwolf's start-up text ends with the network ports it disabled.
            self._banner.append(line)
            if line.startswith("Disabled AGW") or line.startswith("Disabled KISS"):
                self._ready = True
                return [Event("status", "direwolf listening")]
            if looks_like_error(line):
                return [Event("line", line, style="warn")]
            return []
        if not line.strip() or line.startswith("End of file on stdin"):
            return []
        m = PACKET.match(line)
        if m:
            stamp = time.strftime("%H:%M:%S")
            return [Event("line", f"{stamp}  {m.group(1)}")]
        if "audio level" in line:
            return [Event("line", f"          {line.strip()}", style="dim")] if self.values["levels"] else []
        return [Event("line", f"          {line.strip()}", style="dim")]

    def stop(self):
        events = super().stop()
        if not self._ready and self._banner:
            events += [Event("line", b, style="warn") for b in self._banner[-6:]]
        shutil.rmtree(self._dir, ignore_errors=True)
        return events

    def markers(self):
        if int(self.values["baud"]) > 1200:
            return []
        return [(self.values["mark"], "mark"), (self.values["space"], "space")]
