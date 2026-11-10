"""Spectrum & receiver: drive an RTL-SDR like SDR++ or GQRX.

A waterfall and spectrum of everything the dongle hears, a demodulator (WFM, NFM, AM, USB,
LSB, CW) on the frequency you click, and every setting of the dongle. The dongle is
driven through rtl_tcp (this PC's, or a connected relay's); see receiver/source.py.
"""

import logging
import threading
import time

import numpy as np

from ..receiver import dsp
from ..receiver.audio import AudioOut
from ..receiver.source import (SET_AGC, SET_BIAS_TEE, SET_DIRECT_SAMPLING, SET_FREQ, SET_GAIN, SET_GAIN_MODE,
                               SET_OFFSET_TUNING, SET_PPM, FileSource, LocalRtlTcp, RtlTcp, SourceError)
from .base import Event, Mode, Setting, Tuning
from .radio import RADIO_SETTINGS

log = logging.getLogger(__name__)
FRAME = 0.04                    # s between spectrum lines (25 a second)

_radio = tuple(s for s in RADIO_SETTINGS if s.key in ("input", "file", "device"))


class ReceiverMode(Mode):
    id = "receiver"
    name = "Spectrum & receiver"
    group = "RTL-SDR"
    summary = ("A waterfall of the band and a receiver for any signal on it: click to listen in WFM, NFM, AM, "
               "SSB or CW, with every setting of the dongle. Like SDR++ or GQRX.")
    output = "receiver"
    source = "radio"
    rate = dsp.AUDIO_RATE
    requires = ("rtl-sdr",)
    settings = _radio + (
        Setting("file_rate", "Recording sample rate", 2400000, "choice", restart=True,
                choices=tuple((r, f"{r / 1e6:g} MS/s") for r in dsp.RATES),
                help="The rate the recording was made at."),
        Setting("freq_mhz", "Frequency", 101.1, "float", 0.0, 2200.0, 0.0125, 6, suffix=" MHz",
                help="Where you're listening. The dongle is tuned so this sits a quarter of the span off its "
                     "centre, away from its DC spike. Click the waterfall to move within the span."),
        Setting("step", "Tuning step", 100000, "choice",
                choices=((100, "100 Hz"), (1000, "1 kHz"), (5000, "5 kHz"), (6250, "6.25 kHz"), (10000, "10 kHz"),
                         (12500, "12.5 kHz"), (25000, "25 kHz"), (100000, "100 kHz"), (200000, "200 kHz"),
                         (1000000, "1 MHz"))),
        Setting("demod", "Mode", "WFM", "choice", choices=tuple((m, f"{m}: {h[3]}") for m, h in dsp.MODES.items())),
        Setting("bandwidth", "Bandwidth", 180000, "int", 50, 220000, 500, suffix=" Hz",
                help="The demodulator's filter. Choosing a mode sets its usual width."),
        Setting("deemphasis", "De-emphasis (WFM)", 50, "choice",
                choices=((50, "50 µs (Australia, Europe)"), (75, "75 µs (Americas, Korea)"), (0, "off"))),
        Setting("squelch_on", "Squelch", False, "bool"),
        Setting("squelch", "Squelch level", -60, "int", -120, 0, 1, suffix=" dBFS",
                help="Mute while the channel is weaker than this. The meter above the waterfall shows the level."),
        Setting("volume", "Volume", 70, "int", 0, 200, 5, suffix=" %"),
        Setting("mute", "Mute", False, "bool"),
        Setting("rate", "Sample rate", 2400000, "choice",
                choices=tuple((r, f"{r / 1e6:g} MS/s ({r / 1e6 * 0.9:.2f} MHz of spectrum)") for r in dsp.RATES),
                help="How wide a slice of the band the dongle sends. 2.4 MS/s is the most a cheap dongle "
                     "does without dropping samples; through a relay over the internet, lower is gentler."),
        Setting("gain_mode", "Gain", "auto", "choice",
                choices=(("auto", "Automatic (tuner AGC)"), ("manual", "Manual (below)")),
                help="Automatic lets the tuner set its own gain; manual uses the gain below."),
        Setting("gain_db", "Manual gain", 29.7, "choice",
                choices=tuple((g / 10, f"{g / 10:.1f} dB") for g in dsp.R820T_GAINS),
                help="The R820T's gain steps (other tuners use the nearest). Lower it if strong stations "
                     "make ghost signals; raise it for weak ones."),
        Setting("rtl_agc", "RTL AGC", False, "bool",
                help="The RTL2832's own digital gain control. Usually off; can help with very weak signals."),
        Setting("ppm", "Frequency correction", 0, "int", -200, 200, suffix=" ppm", shared=True,
                help="The dongle's crystal error, shared by all RTL-SDR modes (ADS-B can measure it). "
                     "A relay applies its own correction instead."),
        Setting("direct", "Direct sampling (HF)", 0, "choice",
                choices=((0, "Off"), (1, "I branch"), (2, "Q branch (RTL-SDR Blog V3)")),
                help="Below 24 MHz: bypasses the tuner and samples the antenna input directly (Q branch on a "
                     "Blog V3; the V4 has an upconverter instead and doesn't need it)."),
        Setting("offset_tuning", "Offset tuning (E4000)", False, "bool",
                help="Only for E4000 tuners: moves the DC spike out of the way."),
        Setting("bias_tee", "Bias tee (antenna power)", False, "bool",
                help="Puts 4.5 V on the antenna socket for an active antenna or LNA. Only on dongles that "
                     "have one (RTL-SDR Blog V3/V4); never with a DC-shorted antenna."),
        Setting("fft", "FFT size", 4096, "choice",
                choices=((1024, "1024"), (2048, "2048"), (4096, "4096"), (8192, "8192"), (16384, "16384")),
                help="More bins: finer detail, slower to react."),
        Setting("average", "Spectrum averaging", 0.5, "float", 0.0, 0.95, 0.05, 2),
        Setting("peak_hold", "Peak hold", True, "bool"),
    )
    tuning = Tuning(
        sdr_mode="This is the SDR: the RTL-SDR on this PC, or a relay's (Connect relay…)",
        bandwidth="0.24 to 2.88 MHz of spectrum (Sample rate)",
        frequencies=(("88-108 MHz", "FM broadcast (WFM)"), ("118-137 MHz", "airband (AM)"),
                     ("144-148 MHz", "2 m amateur (NFM; SSB around 144.1-144.3)"),
                     ("156-162 MHz", "marine VHF (NFM)"), ("430-450 MHz", "70 cm amateur (NFM)"),
                     ("476-477 MHz", "UHF CB in Australia (NFM)"),
                     ("3.5-30 MHz", "HF amateur and shortwave (SSB/AM): needs direct sampling or a V4")),
        notes="Click a signal on the waterfall to listen; drag to fine-tune. Wheel zooms, right-drag pans, "
              "double-click shows the whole span. Typing a frequency outside the span retunes the dongle.",
    )

    # ---- running ----------------------------------------------------------

    def start(self, values):
        super().start(values)
        v = self.values
        self.src = None
        self._lock = threading.Lock()
        self._frame = None
        self._halt = threading.Event()
        self._last_activity = 0.0
        self._warned = ""
        self.center = self._centre_for(float(v["freq_mhz"]) * 1e6, int(v["rate"]))
        from .. import relay
        bridge = relay.current() if v["input"] == "rtl" else None
        self.via = bridge.name if bridge else ""
        try:
            if v["input"] == "file":
                self.src = FileSource(v["file"], int(v["file_rate"]))
                self.values["rate"] = int(v["file_rate"])
            elif bridge is not None:
                self.src = RtlTcp("127.0.0.1", bridge.local_port)
            else:
                self.src = LocalRtlTcp(v["device"], int(v["rate"]), self.center)
        except SourceError as e:
            raise RuntimeError(str(e)) from None
        self._configure_dongle()
        rate = int(self.values["rate"])
        self.spectrum = dsp.Spectrum(int(v["fft"]), float(v["average"]))
        self.demod = dsp.Demodulator(rate, v["demod"], self._offset(), int(v["bandwidth"]),
                                     int(v["deemphasis"]) * 1e-6, self._squelch())
        self.audio = AudioOut()
        self._thread = threading.Thread(target=self._run, daemon=True, name="receiver")
        self._thread.start()

    def _configure_dongle(self):
        v, s = self.values, self.src
        s.set_rate(int(v["rate"]))
        s.command(SET_FREQ, int(self.center))
        s.command(SET_PPM, int(v["ppm"]))
        self._gain()
        s.command(SET_AGC, int(bool(v["rtl_agc"])))
        s.command(SET_DIRECT_SAMPLING, int(v["direct"]))
        if v["offset_tuning"]:
            s.command(SET_OFFSET_TUNING, 1)
        s.command(SET_BIAS_TEE, int(bool(v["bias_tee"])))

    def _gain(self):
        if self.values["gain_mode"] == "auto":
            self.src.command(SET_GAIN_MODE, 0)
        else:
            self.src.command(SET_GAIN_MODE, 1)
            self.src.command(SET_GAIN, int(round(float(self.values["gain_db"]) * 10)))

    @staticmethod
    def _centre_for(tune, rate):
        return tune - rate / 4                    # the signal a quarter span right of the DC spike

    def _offset(self):
        return float(self.values["freq_mhz"]) * 1e6 - self.center

    def _squelch(self):
        return float(self.values["squelch"]) if self.values["squelch_on"] else None

    def apply(self, values):
        old = self.values
        super().apply(values)
        v = self.values
        if not hasattr(self, "src") or self.src is None:
            return
        s = self.src
        rate = int(v["rate"])
        with self._lock:
            if v["rate"] != old["rate"] and v["input"] != "file":
                s.set_rate(rate)
                self.center = self._centre_for(float(v["freq_mhz"]) * 1e6, rate)
                s.command(SET_FREQ, int(self.center))
                self.demod = dsp.Demodulator(rate, v["demod"], self._offset(), int(v["bandwidth"]),
                                             int(v["deemphasis"]) * 1e-6, self._squelch())
            if v["freq_mhz"] != old["freq_mhz"]:
                tune = float(v["freq_mhz"]) * 1e6
                off = tune - self.center
                # Within the span (and clear of the edges and the DC spike): just move the demodulator.
                if not (2000 < abs(off) < rate * 0.45):
                    self.center = self._centre_for(tune, rate)
                    if v["input"] != "file":
                        s.command(SET_FREQ, int(self.center))
                self.demod.retune(self._offset())
            if v["demod"] != old["demod"]:
                self.demod.set(mode=v["demod"], bandwidth=int(v["bandwidth"]))
            elif v["bandwidth"] != old["bandwidth"]:
                self.demod.set(bandwidth=int(v["bandwidth"]))
            if v["deemphasis"] != old["deemphasis"]:
                self.demod.set(deemphasis=int(v["deemphasis"]) * 1e-6)
            self.demod.squelch = self._squelch()
            if v["gain_mode"] != old["gain_mode"] or v["gain_db"] != old["gain_db"]:
                self._gain()
            for key, cmd in (("rtl_agc", SET_AGC), ("direct", SET_DIRECT_SAMPLING), ("offset_tuning", SET_OFFSET_TUNING),
                             ("bias_tee", SET_BIAS_TEE), ("ppm", SET_PPM)):
                if v[key] != old[key]:
                    s.command(cmd, int(v[key]))
            if v["fft"] != old["fft"]:
                self.spectrum = dsp.Spectrum(int(v["fft"]), float(v["average"]))
            self.spectrum.average = float(v["average"])

    def _run(self):
        last_frame = 0.0
        tail = None
        while not self._halt.is_set():
            data = self.src.read()
            if not data:
                time.sleep(0.01)
                continue
            x = dsp.cu8(data)
            with self._lock:
                demod, spectrum = self.demod, self.spectrum
                v = self.values
            try:
                audio = demod.process(x)
            except Exception:  # a bad block must not end the receiver
                log.exception("demodulator")
                continue
            if not v["mute"] and v["demod"] != "RAW":
                self.audio.play(audio, float(v["volume"]) / 100)
            n = spectrum.size                # the latest n samples, for the next spectrum line
            tail = x[-n:] if len(x) >= n or tail is None else np.concatenate([tail, x])[-n:]
            now = time.monotonic()
            if now - last_frame >= FRAME:
                last_frame = now
                db = spectrum.update(tail)
                if db is not None:
                    self._frame = (db.copy(), self.center, int(v["rate"]), demod.level, demod.open)

    def poll(self):
        events = []
        frame, self._frame = self._frame, None
        if frame is not None:
            db, center, rate, level, open_ = frame
            events.append(Event("spectrum", data={"db": db, "center": center, "rate": rate, "level": level,
                                                  "open": open_, "tune": float(self.values["freq_mhz"]) * 1e6,
                                                  "mode": self.values["demod"],
                                                  "bandwidth": float(self.values["bandwidth"])}))
        now = time.time()
        if now - self._last_activity >= 1.0:
            self._last_activity = now
            events += self._status()
        return events

    def _status(self):
        s, v = self.src, self.values
        problem = s.error or self.audio.error
        quiet = time.monotonic() - s.last_data
        if not problem and quiet > 5:
            problem = f"No samples for {quiet:.0f} s" + (f" from the relay {self.via}" if self.via else "")
        if problem:
            if problem != self._warned:
                self._warned = problem
                return [Event("line", problem, style="warn"), Event("status", problem)]
            return []
        self._warned = ""
        gain = "auto gain" if v["gain_mode"] == "auto" else f"{float(v['gain_db']):.1f} dB"
        where = f"the relay {self.via}" if self.via else ("a recording" if v["input"] == "file" else "this PC's dongle")
        text = (("" if v["input"] == "file" else f"{s.tuner_name} on ") + f"{where} · centre {self.center / 1e6:.4f} MHz · {int(v['rate']) / 1e6:g} MS/s · "
                f"{gain}" + (f" · {s.dropped} blocks dropped (too slow)" if s.dropped else ""))
        return [Event("activity", text)]

    def stop(self):
        if getattr(self, "_halt", None) is not None:
            self._halt.set()
        if getattr(self, "_thread", None) is not None:
            self._thread.join(2)
        for part in ("audio", "src"):
            obj = getattr(self, part, None)
            if obj is not None:
                obj.close()
                setattr(self, part, None)
        return []
