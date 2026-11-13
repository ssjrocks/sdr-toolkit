"""Spectrum & receiver: drive an RTL-SDR like SDR++ or GQRX.

A waterfall and spectrum of everything the dongle hears, a demodulator (WFM, NFM, AM, USB,
LSB, CW) on the frequency you click, and every setting of the dongle. The dongle is
driven through rtl_tcp (this PC's, or a connected relay's); see receiver/source.py.
"""

import collections
import logging
import threading
import time

import numpy as np

from ..receiver import dsp
from ..receiver.audio import AudioOut
from ..receiver import identify, trials
from ..receiver.decode import DECODERS, DecoderHost
from ..receiver.survey import Survey
from ..receiver.source import (SET_AGC, SET_BIAS_TEE, SET_DIRECT_SAMPLING, SET_FREQ, SET_GAIN, SET_GAIN_MODE,
                               SET_OFFSET_TUNING, SET_PPM, FileSource, LocalRtlTcp, RtlTcp, SourceError)
from .base import Event, Mode, Setting, Tuning
from .radio import RADIO_SETTINGS

log = logging.getLogger(__name__)
FRAME = 0.04                    # s between spectrum lines (25 a second)
RECENT = 0.5                    # s of samples kept for identifying signals
AUTO_ID = 3.0                   # s between automatic identifications of the tuned signal
POINT_DECODERS = ("dmr", "pager", "aprs", "tones")    # narrow-FM decoders a decode point can run
MAX_POINTS = 24
POINT_WIDTH = 12500             # Hz: each decode point's channel
IQ_WIDTH = 40000                # Hz: the IQ channel a decoder that wants IQ gets (satellite telemetry)
DISCOVER_EVERY = 2.0            # s between looks for new signals (the explorer)
PROBE_FOR = 20.0                # s a new signal gets to show digital voice before it's let go
IGNORE_FOR = 600.0              # s a signal that wasn't digital voice is left alone
IDLE_DROP = 600.0               # s without a frame before a found channel's decoder is stopped
EDGE_ROOM = 0.02                # of the rate: how far inside the band's edge a clicked signal is put
AFC_EVERY = 0.5                 # s of a point's audio per frequency measurement
AFC_GAIN = 0.5                  # of the measured offset corrected each time
AFC_MAX = 4000                  # Hz either way a point may follow its signal
AFC_SNR = 10.0                  # dB over the band's noise in the spectrum: a signal is there to measure
SAME_SIGNAL = 1500              # Hz: a signal found this close to a known one is that one
GRID = 6250                     # Hz: digital voice channels are at least this far apart
SIDEBAND = 30000                 # Hz: a much weaker signal this close to a found one is its side band
SIDEBAND_DB = 20.0               # dB weaker than its neighbour for that

_radio = tuple(s for s in RADIO_SETTINGS if s.key in ("input", "file", "device"))
DECODER_NAMES = {"morse": "Morse / CW", "rtty": "RTTY", "psk": "PSK31 / 63 / 125", "navtex": "NAVTEX",
                 "ft8": "FT8", "ft4": "FT4", "sstv": "SSTV", "wefax": "Weather fax", "aprs": "APRS / packet",
                 "pager": "Pagers (POCSAG / FLEX)", "tones": "DTMF / CTCSS / DCS",
                 "dmr": "DMR / P25 / digital voice (DSD-FME)", "sattlm": "Satellite telemetry (gr-satellites)"}
DECODER_CHOICES = tuple((k, DECODER_NAMES[k]) for k in DECODERS)


class ReceiverMode(Mode):
    id = "receiver"
    name = "Spectrum & receiver"
    group = "RTL-SDR"
    summary = ("A waterfall of the band and a receiver for any signal on it: click to listen in WFM, NFM, AM, "
               "SSB or CW, with every setting of the dongle. Like SDR++ or GQRX.")
    output = "receiver"
    source = "radio"
    rate = dsp.AUDIO_RATE
    discover = False                # find signals by themselves and decode them (the explorer)
    known_channels: frozenset = frozenset()     # the explorer's channels so far (set by the window)
    explorer = False
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
        Setting("voice_volume", "Decoded voice volume", 100, "int", 0, 150, 5, suffix=" %",
                help="The voice a decoder plays (DMR, P25 and the others through DSD-FME), apart from the "
                     "receiver's own audio above."),
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
        Setting("decoder", "Decode with", "", "choice", choices=(("", "Nothing (just listen)"),) + DECODER_CHOICES,
                help="Run one of the audio decoders on what you're hearing; choosing one also sets the mode and "
                     "bandwidth it wants. Its settings: Decoder settings… above the decoded output."),
        Setting("afc", "Lock decode points onto their signal", True, "bool",
                help="DMR / P25 decode points follow their signal's centre (up to 4 kHz either way) while the "
                     "spectrum shows one there. Off: each stays on its own frequency. Also the Lock button."),
        Setting("auto_id", "Identify automatically", True, "bool",
                help="Every few seconds, guess what the signal you're tuned to is (shown above the waterfall)."),
        Setting("settle_ms", "Survey settle time", 150, "int", 20, 2000, 10, suffix=" ms",
                help="After each hop of the survey, samples are thrown away for this long while the tuner "
                     "settles. Through a relay, 250 ms more is added for samples still on their way."),
        Setting("dwell_ms", "Survey dwell time", 0, "int", 0, 2000, 10, suffix=" ms",
                help="How long the survey measures each slice after settling, keeping the strongest moment: "
                     "longer catches signals that are only on now and then, but each pass takes longer. "
                     "0 is the shortest (a few milliseconds)."),
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
        self.decoder: DecoderHost | None = None
        self._decoder_request = None
        self._decoder_events: list = []
        self.points: dict = {}          # decode points: freq (Hz) -> {"decoder", "host", "muted", "heard"}
        self._point_requests: collections.deque = collections.deque()
        self._point_events: collections.deque = collections.deque()   # added to from other threads too
        self._chz = None                # their channelizer
        self._chz_center = None
        self._discover_at = 0.0
        self._discover_sum = None       # the spectrum lines since the last look, summed (linear power)
        self._discover_n = 0
        self._probing: dict[float, float] = {}     # freq -> when its decode point was added to try it
        self._ignored: dict[float, float] = {}     # freq -> until when it's left alone
        self._afc_spec = None           # the latest spectrum line, for AFC to see whether a signal is there
        self._known: set = set()                   # frequencies the explorer has put decode points on
        self._auto: set = set()                     # decode points the explorer added (it may drop them)
        self._decoder_note = ""
        self._iq_chz = None             # the tuned channel's IQ, for a decoder that wants it
        self._recent: list = []
        self._recent_n = 0
        self._identify_busy = False
        self._listen = None              # Identify's recording of the channel, while it's made
        self._identify_state = None      # what Identify is doing, for the window
        self._identify_shown = 0.0
        self._last_auto = 0.0
        self._identified = None
        self.survey: Survey | None = None
        self._survey_request = None
        self._survey_frame = 0.0
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

    def _nudged(self, tune, rate):
        """A centre for a frequency in view that doesn't fit (in an edge, or on the DC spike): the
        dongle moves only as far as needed, so what was clicked stays about where it was on screen."""
        bw = float(self.values["bandwidth"])
        off = tune - self.center
        if abs(off) <= 2000:                         # the DC spike: step just clear of it
            return tune - (bw / 2 + 2000)
        edge = rate / 2 - bw / 2 - EDGE_ROOM * rate  # inside the edge, a little off the filter's slope
        return tune - np.sign(off) * max(edge, 2000 + 1)

    def _fits(self, off, rate):
        """A channel this far from the dongle's centre can be heard where it is: inside the band the
        dongle sends (right up to its edges) and clear of the DC spike in the middle."""
        return 2000 < abs(off) and abs(off) + float(self.values["bandwidth"]) / 2 <= rate / 2

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
                # Within the span (up to its edges, clear of the DC spike): just move the demodulator,
                # so tuning near an edge doesn't make the band jump.
                if not self._fits(off, rate):
                    self.center = self._nudged(tune, rate) if abs(off) <= rate / 2 else self._centre_for(tune, rate)
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
            if not v.get("afc", True) and old.get("afc", True):
                for pt in self.points.values():          # unlocked: each back on its own frequency
                    pt["afc"] = 0.0
                    pt.pop("afc_acc", None)
                self._chz_center = None                  # its channel placed again, at the next block
            if v.get("voice_volume") != old.get("voice_volume"):
                vol = int(v["voice_volume"])
                if self.decoder is not None:
                    self.decoder.call(lambda mode: getattr(mode, "set_voice_volume", lambda _: None)(vol))
                for pt in list(self.points.values()):
                    self._point_volume(pt)
            if self.survey is not None:                          # takes effect from the next slice
                self.survey.settle = self._settle()
                self.survey.dwell = float(v["dwell_ms"]) / 1000

    def _run(self):
        last_frame = 0.0
        tail = None
        while not self._halt.is_set():
            data = self.src.read()
            if not data:
                time.sleep(0.01)
                continue
            x = dsp.cu8(data)
            if self._survey_step(x):
                continue
            # Under the lock: apply() changes the demodulator in place (a new mode rebuilds its
            # filters), and a block caught halfway through that came out broken.
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
            self._decoder_step(audio, x)
            if self.points or self._point_requests:
                self._points_step(x, time.time())
            if self._listen is not None:
                self._listen_step(x)
            self._recent.append(x)
            self._recent_n += len(x)
            while self._recent_n - len(self._recent[0]) > int(v["rate"]) * RECENT and len(self._recent) > 1:
                self._recent_n -= len(self._recent.pop(0))
            n = spectrum.size                # the latest n samples, for the next spectrum line
            tail = x[-n:] if len(x) >= n or tail is None else np.concatenate([tail, x])[-n:]
            now = time.monotonic()
            if now - last_frame >= FRAME:
                last_frame = now
                db = spectrum.update(tail)
                if db is not None:
                    self._frame = (db.copy(), self.center, int(v["rate"]), demod.level, demod.open)
                    self._afc_spec = (self._frame[0], self.center, int(v["rate"]))
                    if self.discover:            # averaged over DISCOVER_EVERY, so noise peaks even out
                        p = 10 ** (db / 10)
                        self._discover_sum = p if self._discover_sum is None or len(self._discover_sum) != len(p) \
                            else self._discover_sum + p
                        self._discover_n += 1
                        if now - self._discover_at >= DISCOVER_EVERY:
                            self._discover_at = now
                            avg = 10 * np.log10(self._discover_sum / self._discover_n + 1e-20)
                            self._discover_sum, self._discover_n = None, 0
                            self._discover(avg, int(v["rate"]))

    # ---- the explorer: find signals and decode them ------------------------------

    def _discover(self, db, rate):
        """Runs on the receiver's thread: each new narrow signal in view (in two looks running) becomes a
        decode point that tries it for digital voice (let go after PROBE_FOR s if DSD-FME finds nothing)."""
        now = time.time()
        busy = set(self.points) | set(self._probing) | {r[1] for r in self._point_requests if r[0] == "add"}
        n = len(db)

        def level(f):                                    # the strongest bin within 3 kHz of f
            i = int(round((f - self.center) * n / rate + n / 2))
            k = max(1, int(3000 * n / rate))
            return float(db[max(0, i - k): i + k + 1].max()) if 0 <= i < n else -999.0
        found = dsp.find_carriers(db, rate, self.center, max_width=25000,
                                  min_snr=float(self.values.get("discover_snr", 12)), merge=10000)
        strong = [(g, level(g)) for g in list(busy) + found]
        before, self._last_found = getattr(self, "_last_found", []), found
        for f in found:
            if not any(abs(f - g) < 10000 for g in before):
                continue                                 # only in this look: a noise peak or a click so far
            here = level(f)
            if any(0 < abs(f - g) < SIDEBAND and lg - here > SIDEBAND_DB for g, lg in strong):
                continue                                 # a strong neighbour's side band, not a signal
            # Where it is, not the nearest channel of a raster: a transmitter or dongle a few kHz off
            # put the decoder beside the signal. One already known (found again) keeps its frequency.
            f = float(round(f / 100) * 100)
            near = [g for g in self._known | set(self.known_channels) if abs(f - g) < SAME_SIGNAL]
            if near:
                f = min(near, key=lambda g: abs(f - g))
            if abs(f - self.center) + POINT_WIDTH / 2 > rate / 2 or any(abs(f - g) < GRID for g in busy):
                continue
            if any(abs(f - g) < SAME_SIGNAL and until > now for g, until in self._ignored.items()):
                continue
            if len(busy) >= MAX_POINTS:
                break
            self._known.add(f)
            self.add_point(f, "dmr")
            self._probing[f] = now
            self._auto.add(f)
            busy.add(f)
            self._point_events.append((f, "dmr", Event("explorer", data={"freq": f, "state": "probing"})))

    def _digital_check(self, pt, evs):
        """DSD-FME's lines from a point: it carries digital voice once a frame passed its checks (a colour
        code, NAC or call) or HEARD_AFTER clean syncs came in, as Identify decides. Until then a found
        signal's lines are held back, so a stray sync in noise doesn't make it a channel."""
        from .digital_voice import HEARD_AFTER
        for e in evs:
            if e.kind != "dsd":
                continue
            d = e.data
            if d.get("cc") is not None or d.get("nac") or d["what"] == "call":
                pt["verified"] = pt.get("verified", 0) + 1
            elif d["what"] == "frame" and not d.get("error"):
                pt["clean"] = pt.get("clean", 0) + 1
            if pt.get("verified") or pt.get("clean", 0) >= HEARD_AFTER:
                pt["digital"] = time.time()
        if not self.discover or pt.get("digital"):
            out, pt["held"] = pt.get("held", []) + evs, []
            return out
        pt["held"] = (pt.get("held", []) + [e for e in evs if e.kind == "dsd"])[-200:]
        return [e for e in evs if e.kind != "dsd"]

    def _check_found(self, now):
        """The explorer's decode points: keep the ones with digital voice, let the others go."""
        for f, t0 in list(self._probing.items()):
            pt = self.points.get(f)
            if pt is not None and pt.get("digital"):
                del self._probing[f]
            elif now - t0 > PROBE_FOR:
                del self._probing[f]
                self._ignored[f] = now + IGNORE_FOR
                self.remove_point(f)
                self._auto.discard(f)
                self._point_events.append((f, "dmr", Event("explorer", data={"freq": f, "state": "not digital"})))
        for f in list(self._auto):
            pt = self.points.get(f)
            if pt is not None and pt.get("digital") and now - pt["digital"] > IDLE_DROP:
                self.remove_point(f)                     # quiet for a while: found again when it comes back
                self._auto.discard(f)
                self._point_events.append((f, "dmr", Event("explorer", data={"freq": f, "state": "idle"})))

    # ---- identifying the tuned signal ---------------------------------------

    def identify(self, auto=False):
        """Find out what the tuned signal is (in the background); the answer comes as an "identified"
        event. Automatically, it's measured and guessed at (identify.py). From the button, the
        channel is also recorded for a few seconds and put through the decoders (trials.py): what
        decodes comes first, proven."""
        if self._identify_busy or not self._recent:
            return
        x = np.concatenate(list(self._recent))
        rate, tune, center = int(self.values["rate"]), float(self.values["freq_mhz"]) * 1e6, self.center
        self._identify_busy = True
        if auto:
            threading.Thread(target=self._guess, args=(x, rate, tune, center, True), daemon=True,
                             name="identify").start()
            return
        out = trials.capture_rate(rate)
        self._listen = {"tune": tune, "center": center, "rate": rate, "x": x, "out": out, "chunks": [], "n": 0,
                        "mixer": dsp.Mixer(rate, tune - center),
                        "dec": dsp.Decimator(rate, rate // out, out * 0.45) if out != rate else None}
        self._identify_state = {"stage": "listening", "left": trials.LISTEN, "freq": tune}

    def _guess(self, x, rate, tune, center, auto, iq=None, iq_rate=0):
        try:
            m = identify.measure(x, rate, tune - center)
            g = identify.guesses(m, tune / 1e6)
            found = {"freq": tune, "auto": auto, "measurement": m, "guesses": g, "verified": [], "tried": []}
            if iq is not None:
                def progress(done, total):
                    self._identify_state = {"stage": "trying", "done": done, "total": total, "freq": tune}
                self._identify_state = {"stage": "trying", "done": 0, "total": 0, "freq": tune}
                verified, tried = trials.run(iq, iq_rate, tune / 1e6, m.bandwidth, progress)
                found.update(verified=verified, tried=tried, guesses=verified + g)
            self._identified = found
        except Exception:  # identifying failing mustn't touch the receiver
            log.exception("identify")
        finally:
            self._identify_busy = False
            self._identify_state = None

    def _listen_step(self, x):
        """Runs on the receiver's thread: the channel for Identify's decoders, until it's long enough."""
        ls = self._listen
        if float(self.values["freq_mhz"]) * 1e6 != ls["tune"] or self.center != ls["center"]:
            self._listen = None                              # retuned meanwhile: start again
            self._identify_busy = False
            self._identify_state = {"stage": "cancelled", "freq": ls["tune"]}
            return
        y = ls["mixer"](x)
        if ls["dec"] is not None:
            y = ls["dec"](y)
        ls["chunks"].append(y.astype(np.complex64))
        ls["n"] += len(y)
        left = trials.LISTEN - ls["n"] / ls["out"]
        self._identify_state = {"stage": "listening", "left": max(left, 0.0), "freq": ls["tune"]}
        if left <= 0:
            self._listen = None
            iq = np.concatenate(ls["chunks"])
            threading.Thread(target=self._guess, args=(ls["x"], ls["rate"], ls["tune"], ls["center"], False, iq,
                                                       ls["out"]), daemon=True, name="identify").start()

    def set_center(self, hz):
        """The grab bar: slide the dongle along the band. The tuned frequency stays if it's still in
        view, otherwise it moves with the band. Returns the new tuned MHz if it moved."""
        if getattr(self, "src", None) is None:
            return None
        rate = int(self.values["rate"])
        self.center = float(hz)
        if self.values["input"] != "file":
            self.src.command(SET_FREQ, int(self.center))
        tune = float(self.values["freq_mhz"]) * 1e6
        moved = None
        if abs(tune - self.center) + float(self.values["bandwidth"]) / 2 > rate / 2:
            moved = round((self.center + rate / 4) / 1e6, 6)
            self.values["freq_mhz"] = moved
            tune = moved * 1e6
        with self._lock:
            self.demod.retune(tune - self.center)
        return moved

    # ---- the survey (mega-spectrum) ------------------------------------------

    def start_survey(self, lo=None, hi=None):
        """Sweep [lo, hi] (default: the tuner's whole range). The audio and decoder pause meanwhile."""
        self._survey_request = ("start", lo, hi)

    def survey_view(self, lo, hi):
        self._survey_request = ("view", lo, hi)

    def stop_survey(self):
        self._survey_request = ("stop", None, None)

    def _survey_step(self, x) -> bool:
        """Runs on the receiver's thread. True while surveying (the block was the survey's)."""
        req, self._survey_request = self._survey_request, None
        now = time.monotonic()
        if req is not None:
            what, lo, hi = req
            if what == "start":
                self.survey = Survey(int(self.values["rate"]), self.src.tuner, self._settle(),
                                     dwell=float(self.values["dwell_ms"]) / 1000)
                if lo is not None:
                    self.survey.set_view(lo, hi)
                self._survey_tune(now)
            elif what == "view" and self.survey is not None:
                self.survey.set_view(lo, hi)
                self._survey_tune(now)
            elif what == "stop" and self.survey is not None:
                self.survey = None
                self.src.command(SET_FREQ, int(self.center))        # back to where the receiver was
                return True
        sv = self.survey
        if sv is None:
            return False
        if sv.feed(x, now):
            nxt = sv.next_center()
            if nxt != sv.current:
                self._survey_tune(now)
            else:
                sv.tuned(nxt, now - sv.settle)                         # staying put: no settling needed
        return True

    def _settle(self):
        return float(self.values["settle_ms"]) / 1000 + (0.25 if self.via else 0)

    def _survey_tune(self, now):
        sv = self.survey
        c = sv.next_center()
        self.src.command(SET_FREQ, int(c))
        sv.tuned(c, now)

    # ---- the decoder on the receiver's audio --------------------------------

    def set_decoder(self, mode_id, values=None):
        """Start (or stop, with "") a decoder on the audio; values are its saved settings. Runs on
        the receiver's thread at its next block."""
        self._decoder_request = (mode_id, dict(values or {}))

    def decoder_call(self, fn):
        if self.decoder is not None:
            self.decoder.call(fn)

    # ---- decode points: more channels decoded at once ----------------------

    def add_point(self, freq, decoder, values=None, muted=False):
        """Decode `freq` (Hz, inside the span) with a narrow-FM decoder as well, at the same time as
        everything else. Runs on the receiver's thread at its next block."""
        self._point_requests.append(("add", float(freq), decoder, dict(values or {}), muted))

    def remove_point(self, freq):
        self._point_requests.append(("remove", float(freq), None, None, None))

    def mute_point(self, freq, muted):
        self._point_requests.append(("mute", float(freq), None, None, muted))

    def _point_volume(self, pt):
        vol = 0 if pt["muted"] else int(self.values.get("voice_volume", 100))
        pt["host"].call(lambda mode: getattr(mode, "set_voice_volume", lambda _: None)(vol))

    def _stop_point_later(self, freq, pt):
        """A decoder program can take a moment to end: not on the receiver's thread, which would
        fall behind the dongle (and drop samples) meanwhile."""
        def run():
            self._point_events.extend((freq, pt["decoder"], e) for e in pt["host"].stop())
        t = threading.Thread(target=run, daemon=True, name="point-stop")
        t.start()
        self._stopping = [s for s in getattr(self, "_stopping", []) if s.is_alive()] + [t]

    def _points_step(self, x, now):
        rate = int(self.values["rate"])
        while self._point_requests:
            what, freq, decoder, values, muted = self._point_requests.popleft()
            pt = self.points.get(freq)
            if what == "add" and pt is None and len(self.points) < MAX_POINTS:
                from . import MODES
                M = next(m for m in MODES if m.id == decoder)
                vals = {s.key: s.default for s in M.settings}
                vals.update(values)
                if decoder == "dmr":
                    from .digital_voice import RECEIVER_AUDIO
                    vals["input"] = RECEIVER_AUDIO
                    vals["voice_volume"] = 0 if muted else int(self.values.get("voice_volume", 100))
                try:
                    host = DecoderHost(M(), vals)
                except Exception as e:  # missing parts or a bad setting: said, and not added
                    log.info("decode point %s: %s", decoder, e)
                    self._point_events.append((freq, decoder, Event("line", f"Couldn't start {M.name}: {e}",
                                                                     style="warn")))
                    continue
                self.points[freq] = {"decoder": decoder, "host": host, "muted": bool(muted), "heard": 0.0}
                self._chz_center = None                  # place its channel
            elif what == "remove" and pt is not None:
                del self.points[freq]
                if self._chz is not None:
                    self._chz.remove(freq)
                self._stop_point_later(freq, pt)
            elif what == "mute" and pt is not None:
                pt["muted"] = bool(muted)
                self._point_volume(pt)
        if not self.points:
            self._chz = None
            return
        if self._chz is None or self._chz.rate != rate:
            self._chz = dsp.Channelizer(rate)
            self._chz_center = None
        if self._chz_center != self.center:
            # Only the points inside the band the dongle sends get a channel (and audio).
            self._chz_center = self.center
            for freq, pt in self.points.items():
                off = freq - self.center
                if abs(off) + POINT_WIDTH / 2 <= rate / 2:
                    self._chz.set(freq, off + pt.get("afc", 0.0), POINT_WIDTH)
                else:
                    self._chz.remove(freq)
        for freq, y in self._chz.process(x).items():
            pt = self.points.get(freq)
            if pt is not None and len(y):
                if pt["decoder"] == "dmr":
                    self._afc(freq, pt, y, now)
                pt["host"].feed(dsp.fm_discriminator(self._chz.channels[freq], y), now)

    def _signal_at(self, f):
        """Whether the spectrum shows a signal within half a channel of f, AFC_SNR over the noise."""
        spec = self._afc_spec
        if spec is None:
            return False
        db, center, rate = spec
        n = len(db)
        i = int(round((f - center) * n / rate + n / 2))
        k = max(1, int(POINT_WIDTH / 2 * n / rate))
        if not 0 <= i < n:
            return False
        return float(db[max(0, i - k): i + k + 1].max()) - float(np.median(db)) >= AFC_SNR

    def _afc(self, freq, pt, y, now):
        """Automatic frequency control for a digital voice point: the mean of its signal's instantaneous
        frequency is its offset from the point's centre (DMR's and P25's four tones come equally often),
        weighted by power so the gaps between bursts count for little. Measured only while the spectrum
        shows a signal there (even one too far off to decode yet), so noise doesn't walk the point
        away; each time it moves part of the way."""
        ch = self._chz.channels[freq]
        prev = np.concatenate([[ch["last"]], y[:-1]])
        p = np.abs(y) ** 2
        f = np.angle(y * np.conj(prev)) * dsp.AUDIO_RATE / (2 * np.pi)
        acc = pt.setdefault("afc_acc", [0.0, 0.0, 0])
        acc[0] += float(np.sum(p * f))
        acc[1] += float(np.sum(p))
        acc[2] += len(y)
        if acc[2] < AFC_EVERY * dsp.AUDIO_RATE:
            return
        wf, w, _ = acc
        pt["afc_acc"] = [0.0, 0.0, 0]
        if w <= 0 or not self.values.get("afc", True) or not self._signal_at(freq + pt.get("afc", 0.0)):
            return
        afc = float(np.clip(pt.get("afc", 0.0) + AFC_GAIN * wf / w, -AFC_MAX, AFC_MAX))
        if abs(afc - pt.get("afc", 0.0)) >= 20:
            pt["afc"] = afc
            self._chz.set(freq, freq - self.center + afc, POINT_WIDTH)

    def point_states(self) -> list[dict]:
        """For the window: each decode point, whether it's in the band, and when it last decoded."""
        rate = int(self.values["rate"])
        return [{"freq": f, "decoder": pt["decoder"], "muted": pt["muted"], "heard": pt["heard"],
                 "afc": pt.get("afc", 0.0),
                 "in_view": abs(f - self.center) + POINT_WIDTH / 2 <= rate / 2} for f, pt in self.points.items()]

    def _decoder_iq(self, x):
        """The tuned channel as complex samples at 48 kHz, for a decoder that wants IQ (satellite
        telemetry): IQ_WIDTH wide, following the tuning (and so a satellite's Doppler shift)."""
        rate = int(self.values["rate"])
        chz = self._iq_chz
        if chz is None or chz.rate != rate:
            try:
                chz = self._iq_chz = dsp.Channelizer(rate)
            except ValueError as e:                     # a rate that isn't a multiple of 48 kHz
                if self.decoder is not None and not self.decoder.error:
                    self.decoder.error = f"{e}: choose 1.92, 2.4 or 2.88 MS/s for this decoder"
                return np.zeros(0, np.complex64)
        off = self._offset()
        ch = chz.channels.get("iq")
        if ch is None or abs(ch["offset"] - off) > 1:
            chz.set("iq", off, IQ_WIDTH)
        return chz.process(x).get("iq", np.zeros(0, np.complex64))

    def _decoder_step(self, audio, x=None):
        req, self._decoder_request = self._decoder_request, None
        if req is not None:
            if self.decoder is not None:
                self._decoder_events += [(self.decoder.id, e) for e in self.decoder.stop()]
                self.decoder = None
            mode_id, values = req
            if mode_id:
                from . import MODES
                M = next(m for m in MODES if m.id == mode_id)
                vals = {s.key: s.default for s in M.settings}
                vals.update(values)
                if mode_id == "dmr":
                    from .digital_voice import RECEIVER_AUDIO
                    vals["input"] = RECEIVER_AUDIO
                    vals["voice_volume"] = int(self.values.get("voice_volume", 100))
                try:
                    self.decoder = DecoderHost(M(), vals)
                    self._decoder_note = f"Decoding with {M.name}"
                except Exception as e:  # missing parts or a bad setting: say so, keep listening
                    log.info("decoder %s: %s", mode_id, e)
                    from .base import MissingParts
                    parts = f" (needs {', '.join(e.parts)}: install it from the {M.name} page)" \
                        if isinstance(e, MissingParts) else ""
                    self._decoder_note = f"Couldn't start {M.name}: {e}{parts}"
                    self._decoder_events.append((mode_id, Event("line", self._decoder_note, style="warn")))
            else:
                self._decoder_note = ""
        if self.decoder is not None:
            if getattr(self.decoder.mode, "iq", False):
                if x is not None:
                    self.decoder.feed_iq(self._decoder_iq(x), time.time())
            else:
                self.decoder.feed(audio, time.time())
            if self.decoder.error:
                self._decoder_events.append((self.decoder.id, Event("line", self.decoder.error, style="warn")))
                self.decoder.stop()
                self.decoder = None

    def poll(self):
        events = []
        d = self.decoder
        if d is not None:
            evs, tail = d.drain()
            events += [Event("decoded", data={"id": d.id, "event": e}) for e in evs]
            events.append(Event("decoder_audio", data={"id": d.id, "tail": tail, "rate": d.rate,
                                                       "markers": d.mode.markers()}))
        pending, self._decoder_events = self._decoder_events, []
        events += [Event("decoded", data={"id": i, "event": e}) for i, e in pending]
        for freq, pt in list(self.points.items()):
            evs, _ = pt["host"].drain()
            if pt["host"].error:
                evs.append(Event("line", pt["host"].error, style="warn"))
                pt["host"].error = ""
            if any(e.kind in ("line", "row", "log") and e.style not in ("dim", "warn") for e in evs):
                pt["heard"] = time.time()
            if any(e.kind == "dsd" for e in evs):
                evs = self._digital_check(pt, evs)
            self._point_events += [(freq, pt["decoder"], e) for e in evs]
        if self.discover:
            self._check_found(time.time())
        pending = [self._point_events.popleft() for _ in range(len(self._point_events))]
        events += [Event("point_decoded", data={"freq": f, "id": d, "event": e}) for f, d, e in pending]
        frame, self._frame = self._frame, None
        if frame is not None:
            db, center, rate, level, open_ = frame
            events.append(Event("spectrum", data={"db": db, "center": center, "rate": rate, "level": level,
                                                  "open": open_, "tune": float(self.values["freq_mhz"]) * 1e6,
                                                  "mode": self.values["demod"],
                                                  "bandwidth": float(self.values["bandwidth"])}))
            if (self.values.get("auto_id") and time.time() - self._last_auto > AUTO_ID
                    and level > float(np.median(db)) + 6):
                self._last_auto = time.time()
                self.identify(auto=True)
        sv = self.survey
        if sv is not None and time.time() - self._survey_frame >= 0.25:
            self._survey_frame = time.time()
            slices = [sv.done.popleft() for _ in range(len(sv.done))]
            events.append(Event("survey", data={"lo": sv.lo, "hi": sv.hi, "slices": slices, "res": sv.rate / sv.fft,
                                                "pos": sv.current, "pass": sv.last_pass,
                                                "steps": len(sv.centers), "min": sv.min_hz, "max": sv.max_hz}))
        found, self._identified = self._identified, None
        if found is not None:
            events.append(Event("identified", data=found))
        state = self._identify_state
        if state is not None and time.time() - self._identify_shown >= 0.25:
            self._identify_shown = time.time()
            events.append(Event("identifying", data=dict(state)))
            if state["stage"] == "cancelled":
                self._identify_state = None
        now = time.time()
        if now - self._last_activity >= 1.0:
            self._last_activity = now
            events += self._status()
        return events

    def _status(self):
        s, v = getattr(self, "src", None), self.values
        if s is None:                        # not started (no dongle or recording)
            return []
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
                f"{gain}" + (f" · {s.dropped} blocks dropped (too slow)" if s.dropped else "")
                + (f" · {self._decoder_note}" if self._decoder_note else ""))
        return [Event("activity", text)]

    def stop(self):
        try:
            if getattr(self, "_halt", None) is not None:
                self._halt.set()
            if getattr(self, "_thread", None) is not None:
                self._thread.join(5)
            if getattr(self, "decoder", None) is not None:
                self.decoder.stop()
                self.decoder = None
            # The decode points' programs end together, not one after another.
            points, self.points = list(getattr(self, "points", {}).values()), {}
            stoppers = [threading.Thread(target=pt["host"].stop, daemon=True) for pt in points]
            for t in stoppers:
                t.start()
            for t in stoppers + getattr(self, "_stopping", []):
                t.join(5)
            self._stopping = []
        finally:
            for part in ("audio", "src"):                    # the dongle or relay is let go whatever happened
                obj = getattr(self, part, None)
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:  # closing must not keep the other one open
                        log.exception("closing %s", part)
                    setattr(self, part, None)
        return []
