"""DAB+ digital radio: every ensemble on the channels watched, its stations, what's playing (DLS), each station's
slideshow picture (in Details), and listening to a station. Decoded by welle-cli (welle.io) through the engine
(engines/dab.py); the sound comes from welle-cli's MP3 stream, decoded by ffmpeg into the toolkit's sound out."""

import json
import logging
import shutil
import subprocess
import threading
import time

import numpy as np

from .. import system
from .base import Event, MissingParts, Setting, Tuning
from .radio import RADIO_SETTINGS, RadioMode

log = logging.getLogger(__name__)
PRESETS = {
    "au": ("Australia (capital cities: 9A, 9B, 9C; 8B-8D, 9D regional)", "9A,9B,9C,8B,8C,8D,9D"),
    "uk": ("United Kingdom (10B-12D)", "10B,10C,10D,11A,11B,11C,11D,12A,12B,12C,12D"),
    "eu": ("Europe: all of band III (5A-13F)", "all"),
    "custom": ("The channels below", ""),
}


class DABMode(RadioMode):
    id = "dab"
    name = "DAB+ digital radio"
    group = "SDR"
    summary = ("DAB / DAB+ radio on band III: every ensemble and station on the channels watched, what's playing, "
               "each station's slideshow, and listening. Decoded by welle.io.")
    output = "table"
    requires = ("welle.io",)
    key_column = "key"
    columns = (("label", "Station"), ("dls", "Now playing"), ("ensemble", "Ensemble"), ("channel", "Channel"),
               ("pty", "Programme"), ("mode", "Audio"), ("last", "Last heard"), ("key", "ID"))
    settings = RADIO_SETTINGS + (
        Setting("region", "Channels", "au", "choice", restart=True,
                choices=tuple((k, v[0]) for k, v in PRESETS.items())),
        Setting("channels", "Channel list", "9A,9B,9C", "text", restart=True,
                help="Band III blocks (5A-13F), separated by commas: used with Channels set to the channels below. "
                     "Listening to a station keeps the SDR on that station's channel."),
        Setting("dwell", "Time on each channel", 30, "int", 10, 600, 5, suffix=" s", restart=True,
                help="Moving round several channels: how long on each (one with nothing on is left after 8 s)."),
        Setting("volume", "Volume", 80, "int", 0, 100, 5, suffix=" %"),
    )
    tuning = Tuning(
        sdr_mode="welle-cli runs the SDR at 2.048 MS/s on one DAB channel at a time",
        bandwidth="A DAB ensemble is 1.5 MHz wide and carries 10-20 stations",
        frequencies=(("174-240 MHz", "band III DAB channels 5A-13F"),
                     ("202.9-206.4 MHz", "Australia's capital cities: 9A, 9B, 9C")),
        notes="DAB needs a clear signal: indoors near a window, or an outdoor antenna for band III (a dipole of "
              "about 35 cm each side). Stations' slideshows appear in Details.",
    )

    def channel_list(self) -> list[str]:
        from ..engines.dab import BLOCKS
        v = self.values
        text = PRESETS.get(v["region"], PRESETS["custom"])[1] or v["channels"]
        if self._listen_channel:
            return [self._listen_channel]
        if text == "all":
            return list(BLOCKS)
        return [c.strip().upper() for c in text.split(",") if c.strip()]

    _listen_channel = ""

    def listening_on(self):
        chans = self.channel_list()
        return chans[0] if len(chans) == 1 else f"{len(chans)} channels"

    def command(self):
        if shutil.which("welle-cli") is None:
            raise MissingParts(["welle.io"])
        self._services: dict[str, dict] = {}
        self._ensembles: dict[str, dict] = {}
        self._dirty = False
        self._last_table = 0.0
        self._ports: dict[str, int] = {}
        self._now = ""
        slides = system.state_dir() / "dab-slides"
        if self.live():
            b = self.bridge()
            src = f"rtl_tcp,127.0.0.1:{b.local_port}" if b else "rtl_sdr"
        else:
            src = self.file_path()
        cmd = system.python_command("sdr_toolkit.engines.dab") + [
            "--channels", ",".join(self.channel_list()), "--dwell", str(self.values["dwell"]), "--input", src,
            "--slides", str(slides)]
        if self.live() and self.gain_db():
            cmd += ["--gain", self.gain_db()]
        return cmd

    # ---- what it hears ----------------------------------------------------------------------------

    def parse(self, line):
        if not line.startswith("{"):
            problem = self.device_problem(line)
            if problem is not None:
                return problem
            return [Event("line", line.strip(), style="warn")] if "dab:" in line else []
        try:
            e = json.loads(line)
        except ValueError:
            return []
        kind = e.get("event")
        if kind == "channel":
            self._now = e["channel"]
            self._ports[e["channel"]] = e["port"]
            return [Event("status", self.activity())]
        if kind == "empty":
            return [Event("log", f"{e['channel']}: no DAB ensemble", style="dim")]
        if kind == "ensemble":
            new = e["channel"] not in self._ensembles
            self._ensembles[e["channel"]] = e
            if new:
                return [Event("line", f"Ensemble {e['label']} on {e['channel']}"
                                      + (f" (SNR {e['snr']:.0f} dB)" if isinstance(e.get("snr"), (int, float)) else ""))]
            return []
        if kind != "service":
            return []
        key = f"{e['channel']} {e['sid']}"
        s = self._services.setdefault(key, {"first": time.time()})
        old_dls = s.get("dls", "")
        s.update(e)
        s["last"] = time.time()
        self._dirty = True
        events = []
        if e.get("dls") and e["dls"] != old_dls:
            events.append(Event("log", f"{e['label']} ({e['channel']}): {e['dls']}"))
        self._maybe_listen()
        return events

    def poll(self):
        events = super().poll()
        if getattr(self, "_dirty", False) and time.time() - self._last_table >= 1.0:
            self._dirty = False
            self._last_table = time.time()
            events.append(self._table())
        return events

    def _table(self):
        rows = []
        for key, s in sorted(getattr(self, "_services", {}).items(), key=lambda kv: (kv[1]["channel"], kv[1]["label"])):
            rows.append({"key": key, "label": s["label"], "dls": s.get("dls", ""), "ensemble": s.get("ensemble", ""),
                         "channel": s["channel"], "pty": s.get("pty", ""), "mode": s.get("mode", ""),
                         "last": time.strftime("%H:%M:%S", time.localtime(s["last"]))})
        return Event("table", data={"rows": rows})

    def details(self, key):
        s = getattr(self, "_services", {}).get(key)
        if s is None:
            return None
        from ..engines.dab import BLOCKS
        f = [("Station", s["label"]), ("Short name", s.get("short", "")), ("Now playing", s.get("dls", "")),
             ("Ensemble", s.get("ensemble", "")),
             ("Channel", f"{s['channel']} ({BLOCKS.get(s['channel'], 0):.3f} MHz)" if s["channel"] in BLOCKS else s["channel"]),
             ("Service ID", s["sid"]), ("Programme type", s.get("pty", "")), ("Audio", s.get("mode", "")),
             ("Language", s.get("language", ""))]
        ens = getattr(self, "_ensembles", {}).get(s["channel"], {})
        if isinstance(ens.get("snr"), (int, float)):
            f.append(("Ensemble SNR", f"{ens['snr']:.1f} dB"))
        out = {"title": s["label"], "subtitle": f"{s.get('ensemble', '')} · {s['channel']}",
               "fields": [x for x in f if x[1]]}
        if s.get("slide"):
            out["image"] = s["slide"]
            out["image_caption"] = "The station's slideshow"
        return out

    def activity(self):
        n = len(getattr(self, "_services", {}))
        e = len(getattr(self, "_ensembles", {}))
        playing = getattr(self, "_playing", None)
        return (super().activity() + f" · {e} ensembles, {n} stations" + (f" · on {self._now}" if self._now else "")
                + (f" · playing {playing['label']}" if playing else ""))

    # ---- listening ---------------------------------------------------------------------------------

    _listen_key = None
    _playing = None

    def actions(self):
        out = []
        sel = self.selected if self.selected in getattr(self, "_services", {}) else None
        if sel:
            name = self._services[sel]["label"]
            out.append(("listen", f"Listen to {name}", True,
                        "Plays this station (the SDR stays on its channel while you listen)."))
        if self._playing is not None or self._listen_key:
            out.append(("stop_listen", "Stop listening", True, "Stops the sound; moving round the channels again."))
        return out

    def run_action(self, action_id):
        if action_id == "listen" and self.selected in getattr(self, "_services", {}):
            s = self._services[self.selected]
            self._stop_player()
            self._listen_key = self.selected
            if self.live() and (len(self.channel_list()) > 1 or self.channel_list()[0] != s["channel"]):
                self._listen_channel = s["channel"]
                return {"dwell": int(self.values["dwell"])}        # restarts on that channel only
            self._maybe_listen()
        elif action_id == "stop_listen":
            self._stop_player()
            self._listen_key = None
            if self._listen_channel:
                self._listen_channel = ""
                return {"dwell": int(self.values["dwell"])}
        return {}

    def _maybe_listen(self):
        key = self._listen_key
        if not key or self._playing is not None:
            return
        s = getattr(self, "_services", {}).get(key)
        port = getattr(self, "_ports", {}).get(s["channel"]) if s else None
        if s is None or port is None or self._now != s["channel"]:
            return
        url = f"http://127.0.0.1:{port}/mp3/{s['sid']}"
        if shutil.which("ffmpeg") is None:
            log.warning("no ffmpeg: can't play %s", url)
            return
        from ..receiver.audio import AudioOut
        out = AudioOut()
        p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", url, "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._playing = {"label": s["label"], "proc": p, "out": out}

        def pump():
            while True:
                pcm = p.stdout.read(9600)
                if not pcm:
                    break
                audio = np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2").astype(np.float32) / 32768
                out.play(audio, float(self.values.get("volume", 80)) / 100)
        threading.Thread(target=pump, daemon=True, name="dab-audio").start()

    def _stop_player(self):
        pl, self._playing = self._playing, None
        if pl is not None:
            pl["proc"].terminate()
            try:
                pl["proc"].wait(3)
            except subprocess.TimeoutExpired:
                pl["proc"].kill()
            pl["out"].close()

    def stop(self):
        self._stop_player()
        events = super().stop()
        if getattr(self, "_services", None):
            events.append(self._table())
        return events
