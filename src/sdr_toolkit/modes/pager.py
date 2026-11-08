"""Pagers: POCSAG and FLEX, decoded by multimon-ng."""

import re
import time

from .base import Event, Setting, Tuning
from .process import ProcessMode

POCSAG = re.compile(r"^(POCSAG\d+):\s+Address:\s*(\d+)\s+Function:\s*(\d+)\s*(?:(Alpha|Numeric|Skyper):\s?(.*))?$")


class PagerMode(ProcessMode):
    id = "pager"
    name = "Pagers (POCSAG / FLEX)"
    group = "Packet & paging"
    summary = "Paging networks: POCSAG at 512/1200/2400 baud and FLEX. Decoded by multimon-ng."
    rate = 22050
    output = "table"
    columns = (("time", "Time"), ("proto", "Protocol"), ("address", "Address"),
               ("function", "Fn"), ("type", "Type"), ("message", "Message"))
    requires = ("multimon-ng",)
    settings = (
        Setting("p512", "POCSAG 512", True, "bool", restart=True),
        Setting("p1200", "POCSAG 1200", True, "bool", restart=True),
        Setting("p2400", "POCSAG 2400", True, "bool", restart=True),
        Setting("flex", "FLEX", True, "bool", restart=True,
                help="FLEX at 1600 baud decodes from normal FM audio; 3200/6400 need flat audio "
                     "(a WebSDR's filtering often prevents it)."),
        Setting("invert", "Invert signal", False, "bool", restart=True,
                help="POCSAG: try this if nothing decodes. Some receivers' FM audio is upside down."),
        Setting("force", "Message type", "auto", "choice", restart=True,
                choices=(("auto", "As sent (auto)"), ("alpha", "Always text"), ("numeric", "Always numeric"),
                         ("skyper", "Skyper")),
                help="Networks don't always mark text and numeric messages correctly."),
        Setting("charset", "Character set", "US", "choice", restart=True,
                choices=(("US", "US ASCII"), ("DE", "German"), ("FR", "French"), ("SE", "Swedish"),
                         ("SI", "Slovenian"))),
        Setting("bch", "Error correction", 2, "choice", restart=True,
                choices=((0, "Off"), (1, "1 bit"), (2, "2 bits (default)")),
                help="More corrects more errors, and lets through more garbage."),
        Setting("hide_empty", "Hide tone-only pages", False, "bool", restart=True),
        Setting("prune", "Drop unlikely decodes", False, "bool", restart=True,
                help="multimon-ng's heuristic filter for garbage messages."),
        Setting("only", "Only addresses", "", "text",
                help="Comma-separated addresses (capcodes) to show; empty shows all."),
    )
    tuning = Tuning(
        sdr_mode="FM (narrow)",
        bandwidth="12.5 kHz (some older POCSAG uses 25 kHz)",
        frequencies=(
            ("138–175 MHz", "Most paging networks (varies by country)"),
            ("929–932 MHz", "North America"), ("450–470 MHz", "Some local/on-site systems"),
        ),
        notes="Pagers send in bursts: find them as short loud bursts of data noise on the "
              "waterfall. Turn the squelch off. Receiving pager traffic is restricted in some "
              "countries: check your local law.",
    )

    def command(self):
        v = self.values
        cmd = ["multimon-ng", "-t", "raw", "-c", "--timestamp", "-C", v["charset"], "-b", str(v["bch"])]
        for key, demod in (("p512", "POCSAG512"), ("p1200", "POCSAG1200"), ("p2400", "POCSAG2400"),
                           ("flex", "FLEX")):
            if v[key]:
                cmd += ["-a", demod]
        if v["invert"]:
            cmd.append("-i")
        if v["force"] != "auto":
            cmd += ["-f", v["force"]]
        if v["hide_empty"]:
            cmd.append("-e")
        if v["prune"]:
            cmd.append("-u")
        cmd.append("-")
        return cmd

    def parse(self, line):
        if not line.strip() or line.startswith("multimon-ng") or line.startswith("  (C)") \
                or line.startswith("Enabled demodulators"):
            return []
        # --timestamp puts "YYYY-MM-DD HH:MM:SS: " in front.
        m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d): (.*)$", line)
        stamp, body = (m.group(1)[11:], m.group(2)) if m else (time.strftime("%H:%M:%S"), line)
        row = None
        p = POCSAG.match(body)
        if p:
            row = {"time": stamp, "proto": p.group(1), "address": p.group(2), "function": p.group(3),
                   "type": p.group(4) or "Tone", "message": (p.group(5) or "").strip()}
        elif body.startswith("FLEX"):
            parts = [x.strip() for x in body.split("|")]
            if len(parts) >= 7:
                row = {"time": stamp, "proto": f"FLEX {parts[2]}", "address": parts[4].lstrip("0"),
                       "function": parts[3], "type": parts[5], "message": "|".join(parts[6:])}
            else:
                row = {"time": stamp, "proto": "FLEX", "address": "", "function": "", "type": "",
                       "message": body[5:].strip()}
        if row is None:
            return [Event("status", body[:120])]
        only = {a.strip().lstrip("0") for a in self.values["only"].split(",") if a.strip()}
        if only and row["address"].lstrip("0") not in only:
            return []
        return [Event("row", data=row)]
