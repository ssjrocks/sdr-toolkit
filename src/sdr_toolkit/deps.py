"""The outside parts modes need, whether they're installed, and installing them.

System packages come from setup/system.sh, run as root through pkexec (the desktop's
password prompt). gr-lora_sdr and DSD-FME are built by setup/build.sh as the user.
"""

import logging
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from . import system

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


def _radiosonde():
    from .engines.sonde_rx import program
    return program("rs41mod") is not None and program("dft_detect") is not None


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
    "gnss-sdr": ("GNSS-SDR (GPS position fix)", lambda: shutil.which("gnss-sdr") is not None, "system"),
    "rtl-sdr": ("the RTL-SDR tools", lambda: shutil.which("rtl_fm") is not None, "system"),
    "gr-lora_sdr": ("gr-lora_sdr (LoRa)", _lora, "build"),
    "dsd-fme": ("DSD-FME and mbelib (DMR, P25)", _dsd, "mbelib"),
    "radiosonde": ("rs1729's radiosonde decoders (RS41, DFM, M10/M20...)", _radiosonde, "build"),
    "gr-satellites": ("gr-satellites (satellite telemetry)", lambda: shutil.which("gr_satellites") is not None,
                      "system"),
}


# On Windows nothing is installed for the user: where each part comes from instead. The app looks
# for programs in the tools bundled with it, in %LOCALAPPDATA%\SDR Toolkit\tools (for ones you
# add), in WSJT-X's install folder and on PATH.
WINDOWS_HELP = {
    "wsjtx": ("Install WSJT-X (free) from <a href='https://wsjt.sourceforge.io/wsjtx.html'>wsjt.sourceforge.io</a>. "
              "The app finds its jt9 in WSJT-X's install folder."),
    "direwolf": ("Get Dire Wolf for Windows from its <a href='https://github.com/wb2osz/direwolf/releases'>releases</a> "
                 "and put direwolf.exe (with the files beside it) in the tools folder."),
    "multimon-ng": ("Get a Windows build of multimon-ng from its "
                    "<a href='https://github.com/EliasOenal/multimon-ng/releases'>releases</a> and put multimon-ng.exe "
                    "(with any DLLs beside it) in the tools folder."),
    "rtl-433": ("Get rtl_433 for Windows from its <a href='https://github.com/merbanan/rtl_433/releases'>releases</a> "
                "and put rtl_433.exe (with any DLLs beside it) in the tools folder."),
    "dsd-fme": ("Get a Windows build of DSD-FME from its <a href='https://github.com/lwvmobile/dsd-fme/releases'>releases</a> "
                "and put dsd-fme.exe (with the files beside it) in the tools folder. " + MBELIB_NOTICE.split("\n\n")[1]),
    "rtl-sdr": "The RTL-SDR tools come with the app; reinstall it if they're missing.",
    "readsb": "readsb has no Windows version yet: use the Linux version of SDR Toolkit for ADS-B.",
    "gnss-sdr": "GNSS-SDR has no Windows version: use the Linux version of SDR Toolkit for the GPS fix.",
    "gr-lora_sdr": "gr-lora_sdr has no Windows version: use the Linux version of SDR Toolkit for LoRa.",
    "gr-satellites": ("gr-satellites has no Windows package (it runs on GNU Radio, e.g. through radioconda): use "
                      "the Linux version of SDR Toolkit for satellite telemetry."),
    "radiosonde": ("rs1729's decoders (<a href='https://github.com/rs1729/RS'>github.com/rs1729/RS</a>) have no "
                   "Windows release: build rs41mod, dfm09mod, m10m20mod and dft_detect and put them in the tools "
                   "folder, or use the Linux version of SDR Toolkit."),
}


def can_install() -> bool:
    """The app installs parts itself (Linux: apt and the builds); on Windows it says where to get them."""
    return not system.WINDOWS


def windows_help(parts) -> str:
    """Rich text saying where each of *parts* comes from on Windows."""
    return "".join(f"<p><b>{PARTS[p][0]}</b>: {WINDOWS_HELP.get(p, '')}</p>" for p in parts)


def describe(parts) -> str:
    return ", ".join(PARTS[p][0] for p in parts)


def missing(parts=None) -> list[str]:
    return [p for p in (parts if parts is not None else PARTS) if not PARTS[p][1]()]


def setup_script(name: str) -> str:
    return str(resources.files("sdr_toolkit") / "setup" / name)


def log_file() -> Path:
    return system.state_dir() / "setup.log"


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
