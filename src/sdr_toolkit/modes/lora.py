"""LoRa, with Meshtastic and MeshCore decoding. The LoRa receiver is gr-lora_sdr."""

import glob
import importlib.util
import os
import re
import sys
import time
from pathlib import Path

from ..engines import mesh
from .base import Event, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode

LORA_PREFIX = Path.home() / ".local/share/sdr-toolkit/gr-lora_sdr"
OVERSAMPLE = 4

# Meshtastic presets: (display name, SF, bandwidth Hz, coding rate 1-4 = 4/5-4/8)
MESHTASTIC = {
    "LongFast": (11, 250000, 1), "LongModerate": (11, 125000, 4), "LongSlow": (12, 125000, 4),
    "MediumFast": (9, 250000, 1), "MediumSlow": (10, 250000, 1),
    "ShortFast": (7, 250000, 1), "ShortSlow": (8, 250000, 1), "ShortTurbo": (7, 500000, 1),
}
# Meshtastic regions (firmware RDEF): start and end MHz.
REGIONS = {"US": (902.0, 928.0), "ANZ": (915.0, 928.0), "EU_868": (869.4, 869.65), "EU_433": (433.0, 434.0)}
# MeshCore's published defaults: (MHz, SF, bandwidth Hz, coding rate)
MESHCORE = {"EU": (869.618, 8, 62500, 1), "US": (910.525, 7, 62500, 1), "ANZ": (916.575, 7, 62500, 3)}


def djb2(s: str) -> int:
    h = 5381
    for c in s.encode():
        h = (h * 33 + c) & 0xFFFFFFFF
    return h


def meshtastic_freq(region: str, channel_name: str, bw: int) -> float:
    """The default slot: djb2(channel name) mod the number of slots in the region."""
    lo, hi = REGIONS[region]
    n = int((hi - lo) // (bw / 1e6))
    return lo + bw / 2e6 + (djb2(channel_name) % n) * bw / 1e6


def lora_available() -> dict | None:
    """Environment for running gr-lora_sdr from its install prefix, or None if it isn't built."""
    py = glob.glob(str(LORA_PREFIX / "lib*/python3*/*-packages")) + glob.glob(str(LORA_PREFIX / "lib*/*/python3*/*-packages"))
    py = [p for p in py if Path(p, "gnuradio", "lora_sdr").is_dir()]
    if not py:
        # Installed system-wide instead?
        return dict(os.environ) if importlib.util.find_spec("gnuradio.lora_sdr") else None
    libs = [str(p) for p in LORA_PREFIX.glob("lib*") if p.is_dir()] + glob.glob(str(LORA_PREFIX / "lib/*-linux-gnu"))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(py + [env.get("PYTHONPATH", "")]).strip(os.pathsep)
    env["LD_LIBRARY_PATH"] = os.pathsep.join(libs + [env.get("LD_LIBRARY_PATH", "")]).strip(os.pathsep)
    return env


class LoRaMode(RadioMode):
    id = "lora"
    name = "LoRa / Meshtastic / MeshCore"
    group = "RTL-SDR"
    summary = ("LoRa frames, decoded as Meshtastic (texts, positions, node names on the default channel) "
               "or MeshCore (adverts, Public channel). LoRa demodulation by gr-lora_sdr.")
    requires = ("gr-lora_sdr",)
    iq_rate = 1000000
    settings = RADIO_SETTINGS + (
        Setting("preset", "Network", "mt:LongFast", "choice", restart=True,
                choices=tuple((f"mt:{n}", f"Meshtastic {n}") for n in MESHTASTIC)
                + (("meshcore", "MeshCore (region default)"), ("custom", "Other LoRa (settings below)")),
                help="Meshtastic's default is LongFast; MeshCore uses one preset per region."),
        Setting("region", "Region", "ANZ", "choice", restart=True,
                choices=(("ANZ", "Australia / NZ"), ("US", "North America"), ("EU_868", "Europe 868 MHz"),
                         ("EU_433", "Europe 433 MHz (Meshtastic only)")),
                help="Sets the frequency: Meshtastic's default slot, or MeshCore's regional preset."),
        Setting("freq_mhz", "Frequency override", "", "text", restart=True,
                help="MHz, e.g. 916.575. Empty uses the network's default for the region; set it if "
                     "your local mesh uses another slot or preset."),
        Setting("sf", "Spreading factor", 11, "int", 5, 12, restart=True, help="Used with 'Other LoRa'."),
        Setting("bw", "Bandwidth", 250000, "choice", restart=True,
                choices=((62500, "62.5 kHz"), (125000, "125 kHz"), (250000, "250 kHz"), (500000, "500 kHz"))),
        Setting("cr", "Coding rate", 1, "choice", restart=True,
                choices=((1, "4/5"), (2, "4/6"), (3, "4/7"), (4, "4/8"))),
        Setting("sync", "Sync word", "0x2B", "text", restart=True,
                help="0x2B Meshtastic. MeshCore doesn't set one, so it's RadioLib's private default, "
                     "0x12 (not confirmed in MeshCore's docs); 0x34 is LoRaWAN."),
        Setting("mt_channel", "Meshtastic channel name", "LongFast", "text",
                help="The primary channel's name; on a default node it's the preset name."),
        Setting("mt_key", "Meshtastic channel key", "AQ==", "text",
                help="Base64 as the app shows it. AQ== is the public default."),
        Setting("mt_more", "More Meshtastic channels", "", "text",
                help="name=key pairs separated by spaces, to read private channels you have the key for."),
        Setting("mc_keys", "MeshCore channels", "Public=8b3387e9c5cdea6ac9e5edbaa115cd72", "text",
                help="name=key pairs (hex or base64) separated by spaces. Public is the default channel."),
        Setting("hex", "Show frame bytes", False, "bool"),
        Setting("bad_crc", "Show frames with bad CRC", False, "bool"),
        Setting("file_rate", "Recording sample rate", 1000000, "int", 62500, 3200000, 1000, suffix=" Hz",
                restart=True),
        Setting("file_offset", "Signal offset in recording", 0, "int", -1600000, 1600000, 1000, suffix=" Hz",
                restart=True),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR (the toolkit tunes it to the network's frequency)",
        bandwidth="4x the LoRa bandwidth (1 MHz for 250 kHz LoRa, 250 kHz for 62.5 kHz)",
        frequencies=(("906.875 MHz", "Meshtastic LongFast, North America (slot 20)"),
                     ("919.875 MHz", "Meshtastic LongFast, Australia/NZ (slot 20)"),
                     ("869.525 MHz", "Meshtastic, Europe 868"),
                     ("910.525 MHz", "MeshCore, North America preset"),
                     ("916.575 MHz", "MeshCore, Australia preset (narrow)"),
                     ("869.618 MHz", "MeshCore, Europe default")),
        notes="Meshtastic messages on the default channel (key AQ==) can be read by anyone; private "
              "channels show as encrypted unless you add their key. MeshCore direct messages are "
              "end-to-end encrypted; Public channel texts and node adverts are readable. A 915 MHz "
              "(or 868/433) antenna helps; nodes are often sparse, so leave it running.",
    )

    def params(self):
        """(frequency Hz, SF, bandwidth, CR, sync word, preamble)."""
        v = self.values
        preset, region = v["preset"], v["region"]
        if preset.startswith("mt:"):
            name = preset[3:]
            sf, bw, cr = MESHTASTIC[name]
            freq = meshtastic_freq(region, name, bw) * 1e6
            sync, pre = "0x2B", 16
        elif preset == "meshcore":
            f, sf, bw, cr = MESHCORE[{"US": "US", "ANZ": "ANZ"}.get(region, "EU")]
            freq = f * 1e6
            sync, pre = v["sync"] if v["sync"] not in ("", "0x2B") else "0x12", 8
        else:
            sf, bw, cr = v["sf"], int(v["bw"]), int(v["cr"])
            freq, sync, pre = 915e6, v["sync"], 8
        if v["freq_mhz"].strip():
            freq = float(v["freq_mhz"]) * 1e6
        return freq, sf, int(bw), int(cr), sync, pre

    def upstream(self):
        if not self.live():
            return None
        v = self.values
        freq, sf, bw, *_ = self.params()
        cmd = ["rtl_sdr", "-d", self.device_index().lstrip(":"), "-f", str(int(freq)),
               "-s", str(bw * OVERSAMPLE), "-p", str(v["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def command(self):
        env = lora_available()
        if env is None:
            raise RuntimeError("LoRa needs gr-lora_sdr, which isn't built yet. Build it with "
                               "scripts/build-gr-lora-sdr.sh in the SDR Toolkit folder (a few minutes).")
        self._env = env
        freq, sf, bw, cr, sync, pre = self.params()
        live = self.live()
        rate = bw * OVERSAMPLE if live else self.values["file_rate"]
        cmd = [sys.executable, "-u", "-m", "sdr_toolkit.engines.lora_rx", "--input", "-" if live else self.file_path(),
               "--rate", str(rate), "--bw", str(bw), "--sf", str(sf), "--cr", str(cr), "--sync", sync,
               "--preamble", str(pre), "--centre", str(int(freq))]
        if not live and self.values["file_offset"]:
            cmd += ["--offset", str(self.values["file_offset"])]
        self._frame = None
        self._summary = f"{freq / 1e6:.3f} MHz, SF{sf}, {bw / 1000:g} kHz, CR 4/{cr + 4}, sync {sync}"
        self._mt_keys, self._mc_keys = self._keys()
        return cmd

    def env(self):
        # The receiver is our module run with gr-lora_sdr on its paths.
        env = dict(self._env)
        pkg_parent = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join([pkg_parent, env.get("PYTHONPATH", "")]).strip(os.pathsep)
        return env

    def _keys(self):
        v = self.values
        mt = [(v["mt_channel"] or "LongFast", mesh.meshtastic_key(v["mt_key"] or "AQ=="))]
        for pair in v["mt_more"].split():
            if "=" in pair:
                n, k = pair.split("=", 1)
                mt.append((n, mesh.meshtastic_key(k)))
        mc = []
        for pair in v["mc_keys"].split():
            if "=" in pair:
                n, k = pair.split("=", 1)
                mc.append((n, mesh.meshcore_key(k)))
        return mt, mc

    def parse(self, line):
        line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        if line == "LORA_READY":
            return [Event("status", f"listening: {self._summary}")]
        if line.startswith("LORA_MISSING"):
            return [Event("line", "gr-lora_sdr could not be loaded: " + line[13:], style="warn")]
        if line.startswith("rx msg:"):
            try:
                self._frame = bytes(int(x, 16) for x in line[7:].split(",") if x.strip())
            except ValueError:
                self._frame = None
            return []
        if line.startswith("CRC") and self._frame is not None:
            frame, self._frame = self._frame, None
            return self._show(frame, "valid" in line and "invalid" not in line)
        return []

    def _show(self, frame, crc_ok):
        stamp = time.strftime("%H:%M:%S")
        if not crc_ok and not self.values["bad_crc"]:
            return [Event("status", f"{stamp}: a frame with a bad CRC (weak or colliding)")]
        preset = self.values["preset"]
        if preset.startswith("mt:"):
            p = mesh.parse_meshtastic(frame, self._mt_keys)
            text = mesh.describe_meshtastic(p) if p else "too short for Meshtastic"
        elif preset == "meshcore":
            p = mesh.parse_meshcore(frame, self._mc_keys)
            text = mesh.describe_meshcore(p) if p else "not a MeshCore packet"
        else:
            text = f"{len(frame)} bytes"
        events = [Event("line", f"{stamp}  {text}" + ("" if crc_ok else "  (bad CRC)"),
                        style="" if crc_ok else "warn")]
        if self.values["hex"] or preset == "custom":
            events.append(Event("line", "          " + frame.hex(" "), style="dim"))
        return events

    def finished(self, code):
        if not self.live() and code == 0:
            return [Event("status", "recording finished")]
        return super().finished(code)
