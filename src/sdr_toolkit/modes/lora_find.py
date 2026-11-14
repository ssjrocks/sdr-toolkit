"""LoRa finder: every burst in the dongle's band, what it is and what it says, with no settings to
know. The engine (engines/lora_find.py) measures each burst's width, works out its spreading factor
and has gr-lora_sdr decode it with each likely sync word; frames are read as LoRaWAN (sync 0x34),
Meshtastic (0x2B, the default channel's key) or MeshCore (0x12, the Public channel)."""

import json
import time

from .. import system
from ..engines import lorawan, mesh
from .base import Event, MissingParts, Setting, Tuning
from .lora import lora_available
from .radio import RADIO_SETTINGS, RadioMode

QUIET_REPEAT = 60.0             # s: the same non-LoRa signal on the same frequency is listed once in this time


class LoRaFinderMode(RadioMode):
    id = "lora_find"
    name = "LoRa finder (find the settings)"
    group = "RTL-SDR"
    summary = ("Finds the LoRa bursts in view and works out each one's settings (bandwidth, spreading factor, "
               "sync word) by itself, then decodes it: LoRaWAN headers, Meshtastic and MeshCore. Says when a "
               "burst isn't LoRa at all.")
    output = "table"
    requires = ("gr-lora_sdr", "rtl-sdr")
    columns = (("time", "Time"), ("freq", "MHz"), ("what", "What"), ("details", "Decoded"),
               ("settings", "Settings found"), ("width", "Width"), ("dur", "Length"), ("snr", "Signal"))
    settings = RADIO_SETTINGS + (
        Setting("centre_mhz", "Centre frequency", 917.4, "float", 24.0, 1766.0, 0.1, 3, suffix=" MHz", restart=True,
                help="The dongle watches 2.2 MHz around this. In Australia LoRaWAN uplinks are mostly on "
                     "916.8-918.2 MHz (917.4 covers them), downlinks on 923.3-927.5, Meshtastic's LongFast on "
                     "919.875 and MeshCore on 916.575; in the US, 902-928 MHz; in Europe, 863-870 MHz."),
        Setting("rate", "Sample rate", 2400000, "choice", restart=True,
                choices=((2400000, "2.4 MS/s (2.2 MHz of band)"), (2048000, "2.048 MS/s"), (1024000, "1.024 MS/s"))),
        Setting("snr", "Find bursts stronger than", 10, "int", 4, 30, 1, suffix=" dB over the noise", restart=True,
                help="Lower finds weaker bursts, and more noise to try."),
        Setting("show_other", "List signals that aren't LoRa", True, "bool",
                help="Narrow FSK and OOK bursts (meters, sensors, alarms): listed once a minute per frequency."),
        Setting("mt_key", "Meshtastic channel key", "AQ==", "text",
                help="For Meshtastic frames: the default channel's key; another channel's base64 key if you know it."),
        Setting("mc_keys", "MeshCore channels", "Public=8b3387e9c5cdea6ac9e5edbaa115cd72", "text",
                help="name=hex key pairs, separated by spaces."),
        Setting("file_rate", "Recording sample rate", 2400000, "int", 250000, 3200000, 1000, suffix=" Hz",
                restart=True),
    )
    tuning = Tuning(
        sdr_mode="Raw IQ from the RTL-SDR; every burst in the 2.2 MHz around the centre is looked at",
        bandwidth="LoRa is 62.5, 125, 250 or 500 kHz wide; the dongle sees 2.2 MHz at once",
        frequencies=(("916.8-918.2 MHz", "Australia: LoRaWAN uplinks (The Things Network, Helium, others)"),
                     ("923.3-927.5 MHz", "Australia: LoRaWAN downlinks (500 kHz)"),
                     ("919.875 MHz", "Meshtastic LongFast, Australia / NZ"),
                     ("916.575 MHz", "MeshCore, Australia"),
                     ("902-928 MHz", "US: LoRaWAN, Meshtastic (906.875 LongFast)"),
                     ("863-870 MHz", "Europe: LoRaWAN (868.1, 868.3, 868.5...), Meshtastic (869.525)")),
        notes="LoRa shows on the waterfall as diagonal stripes (chirps) filling a 125-500 kHz block; FSK as one or "
              "two thin lines. LoRaWAN payloads are encrypted: only the headers can be read (device address, "
              "frame counter, port; a join request's device EUI). A burst needs to be about 10 dB over the "
              "noise to be decoded.",
    )

    def upstream(self):
        if not self.live():
            return None
        v = self.values
        freq, rate = float(v["centre_mhz"]) * 1e6, int(v["rate"])
        if self.bridge():
            return self.relay_stream(freq, rate)
        cmd = ["rtl_sdr", "-d", self.device_index().lstrip(":"), "-f", str(int(freq)), "-s", str(rate),
               "-p", str(v["ppm"])]
        if self.gain_db():
            cmd += ["-g", self.gain_db()]
        return cmd + ["-"]

    def command(self):
        env = lora_available()
        if env is None:
            raise MissingParts(["gr-lora_sdr"])
        self._env = env
        v = self.values
        rate = int(v["rate"]) if self.live() else int(v["file_rate"])
        self._mt_keys = [("LongFast", mesh.meshtastic_key(v["mt_key"] or "AQ=="))]
        self._mc_keys = [(n, mesh.meshcore_key(k)) for n, k in
                         (p.split("=", 1) for p in str(v["mc_keys"]).split() if "=" in p)]
        self._seen_other: dict = {}
        self._counts = {"lora": 0, "frames": 0, "other": 0}
        return system.python_command("sdr_toolkit.engines.lora_find") + [
            "--input", "-" if self.live() else self.file_path(), "--rate", str(rate),
            "--centre", str(int(float(v["centre_mhz"]) * 1e6)), "--snr", str(v["snr"])]

    def env(self):
        import os
        from pathlib import Path
        env = dict(self._env)
        pkg_parent = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join([pkg_parent, env.get("PYTHONPATH", "")]).strip(os.pathsep)
        return env

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            return problem if problem is not None else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        kind = e.get("event")
        if kind == "ready":
            return [Event("status", f"looking for bursts around {e['centre'] / 1e6:.3f} MHz")]
        if kind == "busy":
            return [Event("status", f"busy: {e['dropped']} bursts not looked at (more than it can try at once)")]
        if kind == "error":
            return [Event("log", f"a burst couldn't be worked out: {e.get('text')}", style="warn")]
        if kind != "burst":
            return []
        return self._burst(e)

    def _burst(self, e):
        now = time.time()
        mhz = f"{e['freq'] / 1e6:.3f}"
        row = {"time": time.strftime("%H:%M:%S"), "freq": mhz, "width": f"{e['width'] / 1000:.1f} kHz",
               "dur": f"{e['dur']:.2f} s", "snr": f"{e.get('snr_db', 0):.0f} dB"}
        if not e["lora"]:
            key = round(e["freq"] / 10000)
            if not self.values["show_other"] or now - self._seen_other.get(key, 0) < QUIET_REPEAT:
                return []
            self._seen_other[key] = now
            self._counts["other"] += 1
            row.update(what="not LoRa", details=e.get("note", ""), settings="")
            return [Event("row", data=row), Event("status", self.activity())]
        self._counts["lora"] += 1
        sync = e.get("sync")
        row["settings"] = (f"SF{e['sf']} · {e['bw'] / 1000:g} kHz" + (f" · sync {sync}" if sync else "")
                           + (" · inverted" if e.get("inverted") else ""))
        frames = e.get("frames") or []
        if not frames:
            row.update(what="LoRa", details=e.get("note") or "no frame decoded")
            return [Event("row", data=row), Event("status", self.activity())]
        events = []
        for fr in frames:
            self._counts["frames"] += 1
            data = bytes.fromhex(fr["hex"])
            what, text = self._frame_text(sync, data)
            if fr.get("crc") is False:
                text += " (bad CRC)"
            events.append(Event("row", data=dict(row, what=what, details=text)))
            events.append(Event("raw", f"{mhz} MHz {row['settings']}: {fr['hex']}"))
        return events + [Event("status", self.activity())]

    def _frame_text(self, sync, data):
        """(what it is, what it says) for a frame, by its sync word."""
        if sync == "0x34":
            f = lorawan.parse(data)
            if f:
                return ("LoRaWAN " + ("uplink" if f.uplink else "downlink"), lorawan.describe(f))
            return "LoRa (LoRaWAN sync)", f"{len(data)} bytes, not a LoRaWAN frame: {data[:16].hex(' ')}"
        if sync == "0x2B":
            p = mesh.parse_meshtastic(data, self._mt_keys)
            return "Meshtastic", mesh.describe_meshtastic(p) if p else f"{len(data)} bytes (too short)"
        if sync == "0x12":
            p = mesh.parse_meshcore(data, self._mc_keys)
            if p:
                return "MeshCore", mesh.describe_meshcore(p)
            return "LoRa (private sync)", f"{len(data)} bytes: {data[:16].hex(' ')}"
        return "LoRa", f"{len(data)} bytes: {data[:16].hex(' ')}"

    def listening_on(self):
        return f"{float(self.values['centre_mhz']):.3f} MHz (±{int(self.values['rate']) * 0.45 / 1e6:.1f} MHz)"

    def activity(self):
        c = getattr(self, "_counts", None)
        counts = (f" · {c['lora']} LoRa bursts ({c['frames']} frames decoded), {c['other']} other signals"
                  if c else "")
        return super().activity() + counts
