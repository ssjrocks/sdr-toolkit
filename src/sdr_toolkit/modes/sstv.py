"""SSTV pictures: the SSTV Decoder app's engine."""

import logging
import time
from pathlib import Path

from PIL import Image as PILImage

from ..engines.sstv.decoder import RATE, SSTVDecoder
from ..engines.sstv.modes import MODES
from .base import Event, Mode, Setting, Tuning

log = logging.getLogger(__name__)


class SSTVMode(Mode):
    id = "sstv"
    name = "SSTV"
    group = "Pictures"
    summary = "Slow-scan TV pictures: Robot, Martin, Scottie, PD and Wraase modes."
    rate = RATE
    output = "image"
    aspect = 4 / 3          # SSTV pictures are 4:3 whatever their pixel count
    settings = (
        Setting("mode", "SSTV mode", "", "choice",
                choices=(("", "Any (automatic)"),) + tuple((m.name, m.name) for m in MODES),
                help="Any starts on the VIS code at the start of each picture. Pick one to ignore the rest."),
        Setting("pickup", "Join pictures part way through", True, "bool",
                help="Start from the line sync pulses when the VIS code was missed (tuned in late, "
                     "or lost in noise). Takes 3–12 s of picture."),
        Setting("save", "Save pictures", True, "bool"),
        Setting("folder", "Save to", str(Path.home() / "Pictures" / "SSTV"), "text"),
        Setting("min_rows", "Save only from", 10, "int", 1, 100, suffix=" % of the picture",
                help="Pictures that ended earlier (signal lost) are not saved."),
    )
    tuning = Tuning(
        sdr_mode="USB on 20 m and up (LSB on 80 m and 40 m); FM on VHF/UHF",
        bandwidth="SSB: 2.8–3 kHz (the picture uses 1100–2300 Hz). FM: narrow, 12.5 kHz or more.",
        frequencies=(
            ("3.845 MHz LSB", "80 m"), ("7.171 MHz LSB", "40 m"), ("14.230 MHz USB", "20 m, the busiest"),
            ("21.340 MHz USB", "15 m"), ("28.680 MHz USB", "10 m"),
            ("145.800 MHz FM", "ISS SSTV events (pass times on ariss.org)"),
        ),
        notes="Tuning on SSB needn't be exact: up to ±250 Hz is measured from the VIS header and "
              "corrected. If colours come out as noise on 80/40 m, try the other sideband.",
    )

    def start(self, values):
        super().start(values)
        self.decoder = SSTVDecoder(self.rate)
        self._configure()

    def apply(self, values):
        super().apply(values)
        self._configure()

    def _configure(self):
        name = self.values["mode"]
        self.decoder.forced = next((m for m in MODES if m.name == name), None)
        self.decoder.pickup = bool(self.values["pickup"])

    def feed(self, audio, now):
        return [self._event(kind, img) for kind, img in self.decoder.feed(audio, now)]

    def stop(self):
        return [self._event(kind, img) for kind, img in self.decoder.stop_image()]

    def _event(self, kind, img):
        m = img.mode
        started = time.strftime("%H:%M:%S", time.localtime(img.started))
        bits = [f"{m.name} {m.width}×{m.height}", f"started {started}"]
        if kind == "done":
            bits.append("complete" if img.complete else f"ended at row {img.rows_done}")
        else:
            bits.append(f"row {img.rows_done} of {m.height}")
        bits += [f"tuning {round(img.offset_hz) + 0:+d} Hz", f"clock {round(img.slant_ppm) + 0:+d} ppm"]
        if not img.vis:
            bits.append("no VIS (joined part way through)")
        title = " · ".join(bits)
        if kind == "done":
            saved = self._save(img)
            if saved:
                title += f" · saved {saved.name}"
        return Event("image", image=img.pixels, title=title, done=kind == "done",
                     row=None if kind == "done" else img.rows_done)

    def _save(self, img):
        if not self.values["save"] or img.rows_done < self.values["min_rows"] / 100 * img.mode.height:
            return None
        folder = Path(self.values["folder"]).expanduser()
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(img.started))
        path = folder / f"{stamp}_{img.mode.name.replace(' ', '')}.png"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            PILImage.fromarray(img.pixels).save(path)
        except OSError as e:
            log.warning("could not save %s: %s", path, e)
            return None
        log.info("saved %s", path)
        return path

    def markers(self):
        return [(1200, "sync"), (1500, "black"), (1900, "VIS"), (2300, "white")]
