"""Keyboard-to-keyboard and broadcast text modes: RTTY, PSK31, NAVTEX."""

import time

from ..engines.navtex import NavtexDecoder
from ..engines.psk import PSKDecoder
from ..engines.rtty import RTTYDecoder
from .base import Event, Mode, Setting, Tuning


class TextStream:
    """Turns decoded characters into finished lines and the line in progress."""

    def __init__(self, width=100):
        self.width = width
        self.line = ""

    def add(self, text: str) -> list[Event]:
        events = []
        for ch in text:
            if ch in "\n":
                events.append(Event("line", self.line))
                self.line = ""
            elif ch == "\r":
                continue
            elif ch.isprintable():
                self.line += ch
                if len(self.line) >= self.width:
                    cut = self.line.rfind(" ", 0, self.width)
                    cut = cut if cut > self.width // 2 else self.width
                    events.append(Event("line", self.line[:cut]))
                    self.line = self.line[cut:].lstrip()
        events.append(Event("live", self.line))
        return events


class RTTYMode(Mode):
    id = "rtty"
    name = "RTTY"
    group = "Text"
    summary = "Radioteletype: Baudot (ITA2) on two tones. Amateur 45.45 baud / 170 Hz, and weather and press stations."
    rate = 8000
    settings = (
        Setting("mark", "Mark tone", 2125, "int", 300, 3500, 5, suffix=" Hz", restart=True,
                help="The lower tone for amateur RTTY on LSB (2125 Hz is the standard AFSK pair "
                     "2125/2295). Click the signal's lower tone on the spectrum to set it."),
        Setting("shift", "Shift", 170, "choice", restart=True,
                choices=((170, "170 Hz (amateur)"), (200, "200 Hz"), (425, "425 Hz"), (450, "450 Hz (DWD weather)"),
                         (850, "850 Hz (older commercial)")),
                help="Distance between the two tones."),
        Setting("baud", "Speed", 45.45, "choice", restart=True,
                choices=((45.45, "45.45 baud (amateur, 60 WPM)"), (50.0, "50 baud (DWD and others)"),
                         (56.88, "56.88 baud"), (74.2, "74.2 baud"), (75.0, "75 baud"), (100.0, "100 baud"))),
        Setting("reverse", "Reverse (space below mark)", False, "bool", restart=True,
                help="On for USB reception of amateur RTTY, or if you get gibberish of letters "
                     "and figures. Off on LSB."),
        Setting("stop_bits", "Stop bits", 1.5, "choice", restart=True,
                choices=((1.0, "1"), (1.5, "1.5"), (2.0, "2"))),
        Setting("unshift", "Back to letters after a space", True, "bool", restart=True,
                help="'Unshift on space': most amateur stations expect it; turn off for weather "
                     "bulletins full of numbers."),
        Setting("figures", "Figures set", "US", "choice", restart=True,
                choices=(("US", "US (amateur)"), ("ITA2", "ITA2 (international)"))),
        Setting("squelch", "Squelch", 0.3, "float", 0.0, 0.95, 0.05, 2,
                help="Ignore the audio unless at least this share of it is in the two tones. "
                     "Lower for weak signals in noise; 0 decodes everything (and prints noise)."),
    )
    tuning = Tuning(
        sdr_mode="LSB for amateur RTTY (or USB with Reverse on); USB for most commercial / weather stations",
        bandwidth="500 Hz–1 kHz around the two tones (wider for 425/850 Hz shift)",
        frequencies=(
            ("3.580–3.600 MHz", "80 m RTTY"), ("7.040–7.050 MHz", "40 m RTTY"),
            ("14.080–14.090 MHz", "20 m RTTY, busy in contests"), ("21.080–21.090 MHz", "15 m"),
            ("4.583, 7.646, 10.1008 MHz", "DWD (German weather), 50 baud, 450 Hz shift: "
                                          "dial 1.9 kHz below on USB; check its current schedule"),
        ),
        notes="Put the two tones on the Mark and Space marks on the spectrum: click the lower "
              "tone to set Mark. If you only get a mess of letters and figures, toggle Reverse.",
    )

    def start(self, values):
        super().start(values)
        v = self.values
        self.dec = RTTYDecoder(self.rate, v["mark"], float(v["shift"]), float(v["baud"]), v["reverse"],
                               float(v["stop_bits"]), v["unshift"], v["figures"], float(v["squelch"]))
        self.text = TextStream()

    def apply(self, values):
        super().apply(values)
        self.dec.squelch = float(values["squelch"])

    def feed(self, audio, now):
        return self.text.add(self.dec.feed(audio))

    def markers(self):
        v = self.values
        space = v["mark"] - float(v["shift"]) if v["reverse"] else v["mark"] + float(v["shift"])
        return [(v["mark"], "mark"), (space, "space")]

    def set_frequency(self, hz):
        self.values["mark"] = int(round(hz / 5) * 5)
        return "mark"


class PSKMode(Mode):
    id = "psk"
    name = "PSK31 / 63 / 125"
    group = "Text"
    summary = "BPSK keyboard chat. Many signals sit side by side: click one on the spectrum to read it."
    rate = 8000
    settings = (
        Setting("freq", "Frequency", 1000, "int", 200, 3500, 1, suffix=" Hz",
                help="The signal's audio frequency. Click its trace on the spectrum."),
        Setting("baud", "Speed", 31.25, "choice", restart=True,
                choices=((31.25, "PSK31"), (62.5, "PSK63"), (125.0, "PSK125"))),
        Setting("afc", "Follow drift (AFC)", True, "bool"),
        Setting("afc_range", "AFC range", 15, "int", 1, 100, suffix=" Hz",
                help="How far AFC may move from where you clicked."),
        Setting("squelch", "Squelch", 0.6, "float", 0.0, 0.95, 0.05, 2,
                help="Hide characters unless this share of the power near the frequency is the "
                     "signal itself. Noise gives about 0.3, a clean signal close to 1; 0 shows everything."),
    )
    tuning = Tuning(
        sdr_mode="USB",
        bandwidth="2.5–3 kHz to see the whole PSK segment, then click one signal",
        frequencies=(("3.580 MHz", "80 m"), ("7.070 MHz", "40 m (7.040 in some regions)"),
                     ("10.142 MHz", "30 m"), ("14.070 MHz", "20 m, the busiest"), ("18.100 MHz", "17 m"),
                     ("21.070 MHz", "15 m"), ("28.120 MHz", "10 m")),
        notes="PSK31 signals are narrow (about 60 Hz) and look like pairs of thin lines on the "
              "waterfall. Click the middle of one; AFC keeps it centred.",
    )

    def start(self, values):
        super().start(values)
        v = self.values
        self.dec = PSKDecoder(self.rate, v["freq"], float(v["baud"]), v["afc"], v["squelch"])
        self._configure()
        self.text = TextStream()

    def apply(self, values):
        old = self.values.get("freq")
        super().apply(values)
        if values["freq"] != old:
            self.dec.set_freq(values["freq"])
        self._configure()

    def _configure(self):
        self.dec.afc = self.values["afc"]
        self.dec.afc_range = float(self.values["afc_range"])
        self.dec.squelch = float(self.values["squelch"])

    def feed(self, audio, now):
        events = self.text.add(self.dec.feed(audio))
        events.append(Event("status", f"tracking {self.dec.freq:.1f} Hz, signal share {self.dec.share:.2f}"))
        return events

    def markers(self):
        f = self.dec.freq if hasattr(self, "dec") else self.values["freq"]
        return [(f, "PSK")]

    def set_frequency(self, hz):
        self.values["freq"] = int(round(hz))
        if hasattr(self, "dec"):
            self.dec.set_freq(self.values["freq"])
        return "freq"


class NavtexMode(Mode):
    id = "navtex"
    name = "NAVTEX"
    group = "Text"
    summary = "Maritime safety broadcasts (SITOR-B, 100 baud): navigation and weather warnings."
    rate = 8000
    settings = (
        Setting("center", "Centre", 1700, "int", 500, 3000, 5, suffix=" Hz", restart=True,
                help="Midway between the two tones. With the dial 1.7 kHz below the station on USB "
                     "it's 1700 Hz."),
        Setting("shift", "Shift", 170, "int", 50, 1000, 5, suffix=" Hz", restart=True),
        Setting("stations", "Only stations", "", "text",
                help="Station letters to show (e.g. 'AEI'); empty shows all. The letter is the first "
                     "after ZCZC."),
        Setting("subjects", "Only subjects", "", "text",
                help="Message types to show: A nav warnings, B met warnings, D search & rescue, "
                     "E forecasts, L more nav warnings... Empty shows all."),
        Setting("outside", "Show text outside messages", False, "bool",
                help="Characters between messages are usually noise or phasing."),
    )
    tuning = Tuning(
        sdr_mode="USB (or CW/USB with a narrow filter)",
        bandwidth="300–500 Hz around 1700 Hz",
        frequencies=(("518 kHz", "International NAVTEX, English: dial 516.3 kHz USB"),
                     ("490 kHz", "National-language NAVTEX: dial 488.3 kHz USB"),
                     ("4209.5 kHz", "HF NAVTEX: dial 4207.8 kHz USB")),
        notes="Each station has 10-minute slots every 4 hours; a message starts with ZCZC and ends "
              "with NNNN. The decoder finds the bit order and polarity by itself.",
    )

    raw_output = True

    def start(self, values):
        super().start(values)
        self.dec = NavtexDecoder(self.rate, float(self.values["center"]), float(self.values["shift"]))
        self.text = TextStream()
        self._recent = ""         # last characters, to spot ZCZC
        self._in_msg = False
        self._header = ""
        self._keep = None         # None until the header is in
        self._body = ""           # message text not shown yet (may end in part of NNNN)

    def feed(self, audio, now):
        events = []
        chars = self.dec.feed(audio)
        if chars.strip():
            # Everything the decoder produced, also between messages (noise, phasing).
            events.append(Event("raw", repr(chars)[1:-1]))
        for ch in chars:
            self._recent = (self._recent + ch)[-4:]
            if not self._in_msg:
                if self._recent == "ZCZC":
                    self._in_msg, self._header, self._keep, self._body = True, "", None, ""
                elif self.values["outside"]:
                    events += self.text.add(ch)
                continue
            if self._keep is None:
                self._header += ch
                head = self._header.strip()
                if len(head) >= 4 or ch == "\n":
                    self._keep = self._wanted(head)
                    if self._keep:
                        events += self.text.add(f"\n{time.strftime('%H:%M:%S')}  ZCZC {head}")
                continue
            self._body += ch
            if self._body.endswith("NNNN"):
                if self._keep:
                    events += self.text.add(self._body[:-4] + "NNNN\n")
                    events.append(Event("line", "", style="dim"))
                self._in_msg, self._body = False, ""
            elif self._keep and len(self._body) > 3:
                # Hold back three characters: they may be the start of NNNN.
                events += self.text.add(self._body[:-3])
                self._body = self._body[-3:]
        events.append(Event("status", ("locked" if self.dec.locked else "searching") +
                            f", {self.dec.errors} characters lost"))
        return events

    def _wanted(self, header):
        h = header.replace(" ", "")
        st, subj = (h[0] if h else ""), (h[1] if len(h) > 1 else "")
        stations = self.values["stations"].replace(" ", "").upper()
        subjects = self.values["subjects"].replace(" ", "").upper()
        return (not stations or st in stations) and (not subjects or subj in subjects)

    def markers(self):
        c, s = self.values["center"], self.values["shift"]
        return [(c - s / 2, "B"), (c + s / 2, "Y")]

    def set_frequency(self, hz):
        self.values["center"] = int(round(hz / 5) * 5)
        return "center"
