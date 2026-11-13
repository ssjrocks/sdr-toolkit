"""HF weather fax charts."""

import logging
import time
from pathlib import Path

from PIL import Image as PILImage

from ..engines.wefax import WefaxDecoder
from .base import Event, Mode, Setting, Tuning

log = logging.getLogger(__name__)


class WefaxMode(Mode):
    id = "wefax"
    name = "Weather fax (WEFAX)"
    group = "Pictures"
    summary = "HF radiofax: weather charts and satellite pictures from met and navy stations."
    rate = 12000
    output = "image"
    settings = (
        Setting("lpm", "Lines per minute", 120, "choice", restart=True,
                choices=((60, "60"), (90, "90"), (120, "120 (almost all stations)"), (240, "240"))),
        Setting("ioc", "IOC", 576, "choice", restart=True,
                choices=((576, "576 (almost all stations)"), (288, "288"))),
        Setting("center", "Centre", 1900, "int", 1000, 3000, 10, suffix=" Hz", restart=True,
                help="Midway between black and white. 1900 Hz with the dial 1.9 kHz below the station."),
        Setting("deviation", "Deviation", 400, "int", 100, 500, 10, suffix=" Hz", restart=True,
                help="Black is centre minus this, white centre plus. 400 is the standard."),
        Setting("auto_start", "Start on the start tone", True, "bool", restart=True,
                help="Off: a chart starts as soon as the mode starts (use if you tuned in late)."),
        Setting("auto_stop", "Stop on the stop tone", True, "bool", restart=True),
        Setting("ppm", "Clock correction", 0.0, "float", -2000, 2000, 10, 0, suffix=" ppm", restart=True,
                help="If the chart slants, change this until lines are vertical: about -100 ppm per "
                     "pixel of drift per 10 lines to the right (positive to the left)."),
        Setting("shift", "Horizontal shift", 0.0, "float", -50, 50, 0.5, 1, suffix=" %", restart=True,
                help="Moves the chart sideways if the left edge isn't where it should be."),
        Setting("invert", "Invert (negative)", False, "bool", restart=True),
        Setting("folder", "Save to", str(Path.home() / "Pictures" / "WEFAX"), "text"),
    )
    tuning = Tuning(
        sdr_mode="USB",
        bandwidth="2.4–3 kHz",
        frequencies=(
            ("Station frequency − 1.9 kHz", "The rule for every station: e.g. 8682.0 kHz → dial 8680.1 kHz USB"),
            ("4235, 6340.5, 9110, 12750 kHz", "NMF Boston (US NWS)"),
            ("4346, 8682, 12786, 17151.2 kHz", "NMC Point Reyes (US NWS)"),
            ("3855, 7880, 13882.5 kHz", "DWD Pinneberg (Germany)"),
            ("5100, 11030, 13920 kHz", "VMC Charleville (Australia BoM)"),
            ("2618.5, 4610, 8040 kHz", "GYA Northwood (UK)"),
        ),
        notes="Stations broadcast on a schedule (published on their websites): look it up for "
              "the next chart. Frequencies above are the published carrier; check the current list.",
    )

    def start(self, values):
        super().start(values)
        v = self.values
        self.dec = WefaxDecoder(self.rate, int(v["lpm"]), int(v["ioc"]), float(v["center"]),
                                float(v["deviation"]), v["auto_start"], v["auto_stop"], float(v["ppm"]),
                                v["invert"])
        self.dec.shift = float(v["shift"]) / 100
        self._started = time.time()
        if not v["auto_start"]:
            self.dec.start_now()

    def feed(self, audio, now):
        events = []
        for kind, _ in self.dec.feed(audio):
            if kind == "start":
                self._started = now
                events.append(Event("status", "start tone: phasing"))
            elif kind == "phased":
                events.append(Event("status", "phased: receiving the chart"))
            elif kind == "rows":
                events.append(self._image(done=False))
            elif kind == "done":
                events.append(self._image(done=True))
        return events

    def stop(self):
        if self.dec.state == "image" and self.dec.rows:
            self.dec.finish()
            return [self._image(done=True)]
        return []

    def _image(self, done):
        n = len(self.dec.rows)
        title = f"WEFAX {self.values['lpm']} LPM · started {time.strftime('%H:%M:%S', time.localtime(self._started))}"
        title += f" · {n} lines"
        path = None
        if done:
            path = self._save()
            title += f" · saved {path.name}" if path else ""
        return Event("image", image=self.dec.picture(), title=title, done=done, row=None if done else n,
                     data={"path": str(path)} if path else {})        # for the logbook's pictures

    def _save(self):
        if len(self.dec.rows) < 20:
            return None
        folder = Path(self.values["folder"]).expanduser()
        path = folder / f"{time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime(self._started))}_WEFAX.png"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            PILImage.fromarray(self.dec.picture()[:, :, 0]).save(path)
        except OSError as e:
            log.warning("could not save %s: %s", path, e)
            return None
        return path

    def markers(self):
        c, d = self.values["center"], self.values["deviation"]
        return [(c - d, "black"), (c + d, "white")]
