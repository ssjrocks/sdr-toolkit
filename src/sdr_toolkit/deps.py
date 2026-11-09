"""The outside parts modes need, whether they're installed, and installing them.

System packages come from setup/system.sh, run as root through pkexec (the desktop's
password prompt). gr-lora_sdr and DSD-FME are built by setup/build.sh as the user.
"""

import logging
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)
MBELIB_NOTICE = (
    "DMR / P25 voice needs mbelib, the AMBE/IMBE voice codec library. Its authors' notice:\n\n"
    "\"This source code is provided for educational purposes only. It is a written description of how "
    "certain voice encoding/decoding algorithms could be implemented. Executable objects compiled or "
    "derived from this package may be covered by one or more patents. Readers are strongly advised to "
    "check for any patent restrictions or licencing requirements before compiling or using this source "
    "code.\"\n\nBuild it?"
)


def _lora():
    from .modes.lora import lora_available
    return lora_available() is not None


def _dsd():
    from .modes.digital_voice import dsd_fme
    return dsd_fme() is not None


# part: (what it is, installed?, how it's installed: "system", "build" or "mbelib")
PARTS = {
    "wsjtx": ("WSJT-X's jt9 decoder (FT8, FT4)", lambda: shutil.which("jt9") is not None, "system"),
    "direwolf": ("direwolf (APRS, AIS)", lambda: shutil.which("direwolf") is not None, "system"),
    "multimon-ng": ("multimon-ng (pagers)", lambda: shutil.which("multimon-ng") is not None, "system"),
    "rtl-433": ("rtl_433 (ISM sensors)", lambda: shutil.which("rtl_433") is not None, "system"),
    "readsb": ("readsb (ADS-B)", lambda: shutil.which("readsb") is not None, "system"),
    "rtl-sdr": ("the RTL-SDR tools", lambda: shutil.which("rtl_fm") is not None, "system"),
    "gr-lora_sdr": ("gr-lora_sdr (LoRa)", _lora, "build"),
    "dsd-fme": ("DSD-FME and mbelib (DMR, P25)", _dsd, "mbelib"),
}


def describe(parts) -> str:
    return ", ".join(PARTS[p][0] for p in parts)


def missing(parts=None) -> list[str]:
    return [p for p in (parts if parts is not None else PARTS) if not PARTS[p][1]()]


def setup_script(name: str) -> str:
    return str(resources.files("sdr_toolkit") / "setup" / name)


def log_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
    path = Path(base) / "sdr-toolkit"
    path.mkdir(parents=True, exist_ok=True)
    return path / "setup.log"


def install(parts, accept_mbelib: bool, on_line=lambda s: None) -> list[str]:
    """Install *parts*; returns those still missing afterwards. Runs for minutes: call from a thread.

    Lines starting "STEP " are progress for the user; everything goes to setup.log.
    """
    kinds = {PARTS[p][2] for p in parts}
    builds = kinds & {"build", "mbelib"}
    steps = []
    # The builds need compilers and -dev packages, which system.sh also installs.
    if "system" in kinds or builds:
        steps.append(["pkexec", "bash", setup_script("system.sh")])
    if builds:
        steps.append(["bash", setup_script("build.sh")] + (["--accept-mbelib"] if accept_mbelib else []))
    with open(log_file(), "a") as logf:
        for cmd in steps:
            logf.write(f"$ {' '.join(cmd)}\n")
            logf.flush()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    bufsize=1)
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
                on_line(line.rstrip())
            code = proc.wait()
            if code in (126, 127) and cmd[0] == "pkexec":
                on_line("STEP The password prompt was cancelled; nothing was installed")
                break
            if code != 0:
                on_line(f"STEP {Path(cmd[-1] if cmd[-1].endswith('.sh') else cmd[1]).name} failed "
                        f"(exit code {code}); the details are in {log_file()}")
                break
    return missing(parts)
