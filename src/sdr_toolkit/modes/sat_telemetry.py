"""Satellite telemetry, decoded by gr-satellites from the receiver's IQ.

gr-satellites knows hundreds of satellites by NORAD number (their modulation, framing and
telemetry layout, in its SatYAML files). For one it doesn't know, a SatYAML is written from the
transmitter's modulation and speed (SatNOGS' data), which decodes the many that send plain AX.25.
The receiver gives it a 48 kHz IQ channel around the tuned frequency (followed through the
Doppler shift), as interleaved 16-bit samples on its stdin. It prints each packet as
"-> Packet from <transmitter>" and then the decoded fields, indented; each packet becomes one
line here (the full dump is in Raw data).
"""

import functools
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

from .base import Event, Setting, Tuning
from .process import ProcessMode

PACKET = re.compile(r"^-> Packet from (.*)$")
FIELD = re.compile(r"^(\s*)(\w+) = (.*)$")
QUIET = 0.4                     # s with no new line: the packet's dump is complete
GENERIC = ("FSK", "AFSK")       # modulations a SatYAML can be written for (plain AX.25 framing)
FSK_LIKE = ("FSK", "GFSK", "GMSK", "MSK")


@functools.lru_cache(maxsize=1)
def satyaml_dir() -> Path | None:
    """gr-satellites' SatYAML folder, found through the Python it runs on."""
    exe = shutil.which("gr_satellites")
    if exe is None:
        return None
    try:
        first = Path(exe).read_text(errors="replace").splitlines()[0]
        python = first[2:].strip().split()[-1] if first.startswith("#!") else "python3"
        python = python if python != "env" else "python3"
        out = subprocess.run([python, "-c", "import os, satellites.satyaml as s; print(os.path.dirname(s.__file__))"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, IndexError, subprocess.SubprocessError):
        return None
    return Path(out) if out and Path(out).is_dir() else None


@functools.lru_cache(maxsize=1)
def known_norads() -> frozenset:
    """The NORAD numbers gr-satellites has a SatYAML for."""
    folder = satyaml_dir()
    if folder is None:
        return frozenset()
    out = set()
    for p in folder.glob("*.yml"):
        m = re.search(r"^norad:\s*(\d+)", p.read_text(errors="replace"), re.M)
        if m:
            out.add(int(m.group(1)))
    return frozenset(out)


def generic_modulation(mode: str, baud) -> str:
    """The SatYAML modulation for a SatNOGS mode it can be written for, or ""."""
    m = (mode or "").upper().split()[0] if mode else ""
    if not baud or not 300 <= float(baud) <= 19200:
        return ""
    if m in FSK_LIKE:
        return "FSK"
    if m == "AFSK" and float(baud) == 1200:
        return "AFSK"
    return ""


def can_decode(norad, mode, baud) -> bool:
    return (norad in known_norads()) or bool(generic_modulation(mode, baud))


def satyaml(name, modulation, baud, freq_hz) -> str:
    """A SatYAML for one plain AX.25 downlink (FSK: G3RUH-scrambled at 9600 baud and above)."""
    baud = int(float(baud))
    lines = [f"name: {name or 'Satellite'}", "norad: 99999", "data:", "  &tlm Telemetry:", "    telemetry: ax25",
             "transmitters:", f"  {baud} baud {modulation} downlink:", f"    frequency: {float(freq_hz or 437e6):.6e}",
             f"    modulation: {modulation}", f"    baudrate: {baud}"]
    if modulation == "AFSK":
        lines += ["    af_carrier: 1700", "    deviation: 500", "    framing: AX.25"]
    else:
        lines += ["    deviation: 3000" if baud >= 9600 else "    deviation: 1500",
                  "    framing: AX.25 G3RUH" if baud >= 9600 else "    framing: AX.25"]
    lines += ["    data:", "    - *tlm"]
    return "\n".join(lines) + "\n"


class SatTelemetryMode(ProcessMode):
    id = "sattlm"
    name = "Satellite telemetry (gr-satellites)"
    group = "Satellites"
    hidden = True                       # runs on Spectrum & receiver's IQ, from the satellite page
    summary = ("Telemetry from satellites: gr-satellites decodes the packets (AX.25 callsigns and text, and the "
               "telemetry fields of the satellites it knows).")
    rate = 48000
    iq = True                           # wants the receiver's IQ channel, not demodulated audio
    requires = ("gr-satellites",)
    settings = (
        Setting("satellite", "Satellite", "", "text", restart=True,
                help="Its NORAD number (or name, as gr-satellites knows it). Set for you when you listen from the "
                     "satellite page."),
        Setting("modulation", "Modulation", "FSK", "choice", restart=True,
                choices=(("FSK", "FSK / GFSK / GMSK"), ("AFSK", "AFSK (1200 baud, Bell 202)")),
                help="Used when gr-satellites doesn't know the satellite: a plain AX.25 downlink is assumed."),
        Setting("baud", "Speed", 9600, "int", 300, 19200, 100, suffix=" baud", restart=True,
                help="Used when gr-satellites doesn't know the satellite. 9600 baud FSK is G3RUH-scrambled."),
        Setting("freq", "Downlink", 0, "int", 0, 2e9, 1, suffix=" Hz", restart=True,
                help="Only written into the SatYAML for a satellite gr-satellites doesn't know."),
        Setting("sat_name", "Name", "", "text", restart=True, help="Shown with its packets."),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ around the downlink (Spectrum & receiver follows the Doppler shift)",
        bandwidth="A 40 kHz channel; up to 19200 baud",
        frequencies=(("435-438 MHz", "most amateur cubesats"), ("145.8-146 MHz", "some amateur satellites")),
        notes="Start it a minute before the pass and leave it running: packets come every few seconds to a minute, "
              "and only while the satellite is well up (above 10-15°). A vertical or a small Yagi helps a lot.",
    )

    def command(self):
        v = self.values
        self._dir = Path(tempfile.mkdtemp(prefix="sdr-toolkit-sattlm-"))
        sat = str(v.get("satellite") or "").strip()
        self._generic = False
        if not (sat.isdigit() and int(sat) in known_norads()) and not (sat and not sat.isdigit()):
            mod = v.get("modulation") or "FSK"
            path = self._dir / "satellite.yml"
            path.write_text(satyaml(v.get("sat_name") or sat, mod, v.get("baud") or 9600, v.get("freq")))
            sat = str(path)
            self._generic = True
        self._packet: list[str] = []
        self._packet_from = ""
        self._last_line = 0.0
        self.packets = 0
        return ["gr_satellites", sat, "--rawint16", "/dev/stdin", "--iq", "--samp_rate", str(self.rate)]

    def env(self):
        return dict(os.environ, PYTHONUNBUFFERED="1")    # its packets as they come, not when a buffer fills

    def workdir(self):
        return str(self._dir)

    def feed(self, iq, now):
        """The receiver's complex channel at 48 kHz, as interleaved 16-bit I/Q."""
        if self._proc.poll() is not None or not len(iq):
            return []
        out = np.empty(2 * len(iq), "<i2")
        out[0::2] = np.clip(iq.real, -1, 1) * 32767
        out[1::2] = np.clip(iq.imag, -1, 1) * 32767
        try:
            self._proc.stdin.write(out.tobytes())
        except (BrokenPipeError, OSError):
            pass
        return []

    def poll(self):
        events = super().poll()
        if self._packet and time.monotonic() - self._last_line > QUIET:
            events += self._flush()
        return events

    def parse(self, line):
        self._last_line = time.monotonic()
        m = PACKET.match(line)
        if m:
            out = self._flush()
            self._packet_from = m.group(1).strip()
            self._packet = []
            return out
        if self._packet_from:
            self._packet.append(line)
            return []
        if line.strip():
            return [Event("line", line, style="dim")]        # its start-up messages
        return []

    def _flush(self):
        if not self._packet_from:
            return []
        lines, src = self._packet, self._packet_from
        self._packet, self._packet_from = [], ""
        self.packets += 1
        text = describe(lines)
        who = self.values.get("sat_name") or str(self.values.get("satellite") or "")
        head = f"{who}: " if who else ""
        data = {"satellite": who, "transmitter": src, "text": text}
        return [Event("line", f"{head}{text}"), Event("telemetry", data=data)]

    def activity(self):
        return f"{super().activity()} · {self.packets} packets"


def describe(lines) -> str:
    """A packet's dump in one line: an AX.25 frame as "From X to Y: text", anything else as its
    first fields ("batt_voltage 7.92 · temp 21 · ...") or its size."""
    fields = []
    for ln in lines:
        m = FIELD.match(ln)
        if m:
            fields.append((len(m.group(1)), m.group(2), m.group(3).strip()))
    calls = [_value(v) for _, k, v in fields if k == "callsign"]
    ssids = [v for _, k, v in fields if k == "ssid" and v.isdigit()]
    info = next((_value(v) for _, k, v in fields if k == "info"), None)
    if len(calls) >= 2:
        def call(i):
            return calls[i] + (f"-{ssids[i]}" if i < len(ssids) and ssids[i] != "0" else "")
        out = f"From {call(1)} to {call(0)}"
        if len(calls) > 2:
            out += " via " + ",".join(calls[2:])
        rest = [f"{k} {_value(v)}" for _, k, v in fields
                if k not in ("callsign", "ssid", "ch", "extension", "control", "pid", "info", "header",
                             "addresses") and not v.startswith("Container") and not v.startswith("ListContainer")]
        if info is not None:
            out += f": {info}"
        elif rest:
            out += ": " + " · ".join(rest[:8])
        return out
    leaves = [f"{k} {_value(v)}" for _, k, v in fields
              if not v.startswith("Container") and not v.startswith("ListContainer")]
    if leaves:
        return " · ".join(leaves[:10]) + (f" · (+{len(leaves) - 10} more in Raw data)" if len(leaves) > 10 else "")
    hexes = [ln for ln in lines if re.match(r"^\s*[0-9a-f]{4}:", ln)]
    if hexes:
        return f"{len(hexes) * 16} bytes (no layout known): " + " ".join(lines[0].split()[1:9])
    return "a packet (see Raw data)"


def _value(v: str) -> str:
    """A Construct value without its decoration: u'TEST' (total 4) -> TEST; b'hi' (total 2) -> hi."""
    v = re.sub(r"\s*\(total \d+\)$", "", v.strip())
    m = re.match(r"^[ub]?'(.*)'$", v) or re.match(r'^[ub]?"(.*)"$', v)
    if m:
        v = m.group(1)
    return v.strip()
