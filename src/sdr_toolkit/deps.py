"""The outside parts modes need, whether they're installed, and installing them.

System packages come from setup/system.sh, run as root through pkexec (the desktop's
password prompt). gr-lora_sdr and DSD-FME are built by setup/build.sh as the user.
"""

import logging
import json
import shutil
import sys
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


def _hackrf_library() -> bool:
    import ctypes.util
    return ((system.tools_dir() / "lib" / "libhackrf.so.0").is_file() or ctypes.util.find_library("hackrf") is not None
            or sys.platform == "win32")                                              # bundled on Windows


def _rtlamr():
    from .modes.meters import rtlamr
    return rtlamr() is not None


def _aero():
    from .modes.aero import tool
    return all(tool(b) for b in ("acarsdec", "dumpvdl2", "dumphfdl"))


# part: (what it is, installed?, how it's installed: "bundle" (the tools bundle, in the app's folder), "system"
# (Ubuntu's packages), "build" or "mbelib")
PARTS = {
    "wsjtx": ("WSJT-X's jt9 decoder (FT8, FT4)", lambda: shutil.which("jt9") is not None, "bundle"),
    "direwolf": ("direwolf (APRS, AIS)", lambda: shutil.which("direwolf") is not None, "bundle"),
    "multimon-ng": ("multimon-ng (pagers)", lambda: shutil.which("multimon-ng") is not None, "bundle"),
    "rtl-433": ("rtl_433 (ISM sensors)", lambda: shutil.which("rtl_433") is not None, "bundle"),
    "readsb": ("readsb (ADS-B)", lambda: shutil.which("readsb") is not None, "bundle"),
    "gnss-sdr": ("GNSS-SDR (GPS position fix)", lambda: shutil.which("gnss-sdr") is not None, "bundle"),
    "rtl-sdr": ("the RTL-SDR tools", lambda: shutil.which("rtl_fm") is not None, "bundle"),
    "hackrf": ("libhackrf (a HackRF on this PC)", lambda: _hackrf_library(), "bundle"),
    "gr-lora_sdr": ("gr-lora_sdr (LoRa)", _lora, "bundle"),
    "dsd-fme": ("DSD-FME and mbelib (DMR, P25)", _dsd, "mbelib"),
    "radiosonde": ("rs1729's radiosonde decoders (RS41, DFM, M10/M20...)", _radiosonde, "bundle"),
    "gr-satellites": ("gr-satellites (satellite telemetry)", lambda: shutil.which("gr_satellites") is not None,
                      "bundle"),
    "rtlamr": ("rtlamr (ERT utility meters)", lambda: _rtlamr(), "bundle"),
    "aero": ("acarsdec, dumpvdl2 and dumphfdl (aircraft messages)",
             lambda: _aero(), "bundle"),
    "wmbusmeters": ("rtl_wmbus and wmbusmeters (Wireless M-Bus meters)",
                    lambda: shutil.which("wmbusmeters") is not None and shutil.which("rtl_wmbus") is not None,
                    "bundle"),
    "tshark": ("tshark (Wi-Fi frames)", lambda: shutil.which("tshark") is not None, "system"),
    "satdump": ("SatDump (METEOR-M pictures, Inmarsat)", lambda: shutil.which("satdump") is not None, "bundle"),
    "welle.io": ("welle.io and ffmpeg (DAB+ radio)",
                 lambda: shutil.which("welle-cli") is not None and shutil.which("ffmpeg") is not None, "bundle"),
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
    "hackrf": "libhackrf comes with the app; reinstall it if it's missing.",
    "readsb": "readsb has no Windows version yet: use the Linux version of SDR Toolkit for ADS-B.",
    "gnss-sdr": "GNSS-SDR has no Windows version: use the Linux version of SDR Toolkit for the GPS fix.",
    "gr-lora_sdr": "gr-lora_sdr has no Windows version: use the Linux version of SDR Toolkit for LoRa.",
    "rtlamr": ("ERT meters aren't decoded on Windows yet: use the Linux version of SDR Toolkit."),
    "aero": ("Aircraft messages (ACARS, VDL2, HFDL) aren't decoded on Windows yet: use the Linux version."),
    "wmbusmeters": ("Wireless M-Bus meters aren't decoded on Windows yet: use the Linux version of SDR Toolkit."),
    "tshark": ("Wi-Fi monitor mode isn't available on Windows (its drivers don't offer it): use the Linux version."),
    "satdump": ("SatDump has its own Windows version (satdump.org), not yet used by SDR Toolkit: use the Linux "
                "version for METEOR-M and Inmarsat."),
    "welle.io": ("DAB+ radio isn't on Windows yet: use the Linux version of SDR Toolkit (or welle.io's own Windows "
                 "program)."),
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
    """Parts not there; on Linux a bundle part also counts as missing while the unpacked bundle isn't the one this
    release names (the Install button then fetches it)."""
    stale = not system.WINDOWS and tools_info() is not None and not tools_current()
    return [p for p in (parts if parts is not None else PARTS)
            if not PARTS[p][1]() or (stale and PARTS[p][2] == "bundle")]


def setup_script(name: str) -> str:
    return str(resources.files("sdr_toolkit") / "setup" / name)


def log_file() -> Path:
    return system.state_dir() / "setup.log"


TOOLS_INFO = "tools_bundle.json"          # {"version", "url", "sha256", "size"}: the bundle this release uses


def tools_info() -> dict | None:
    try:
        return json.loads((resources.files("sdr_toolkit") / TOOLS_INFO).read_text())
    except (OSError, ValueError):
        return None


def tools_current() -> bool:
    """The tools bundle unpacked here is the one this release names."""
    info = tools_info()
    try:
        have = (system.tools_dir() / "VERSION").read_text().strip()
    except OSError:
        return False
    return info is None or have == info.get("version")


def install_tools(on_line=lambda s: None, source: str | None = None) -> bool:
    """Download (or take *source*, a local .tar.xz) the tools bundle, check it, and unpack it into the app's folder,
    replacing the old one. Nothing goes outside the app's folder."""
    import hashlib
    import tarfile
    import urllib.request
    info = tools_info() or {}
    dest = system.tools_dir()
    cache = system.cache_dir() / "tools-bundle.tar.xz"
    if source is None:
        url = info.get("url")
        if not url:
            on_line("STEP No tools bundle is named for this release")
            return False
        on_line(f"STEP Downloading the decoders ({info.get('size', 0) / 1e6:.0f} MB)")
        tmp = cache.with_suffix(".part")
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                got, last = 0, 0
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    if got - last >= 25 << 20:
                        last = got
                        on_line(f"STEP Downloading the decoders: {got / 1e6:.0f} of {info.get('size', 0) / 1e6:.0f} MB")
        except OSError as e:
            on_line(f"STEP The download failed: {e}")
            return False
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        if info.get("sha256") and h.hexdigest() != info["sha256"]:
            on_line("STEP The download didn't check out (its checksum is wrong): nothing was changed")
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(cache)
        source = str(cache)
    on_line("STEP Unpacking the decoders")
    new = dest.with_name(dest.name + ".new")
    old = dest.with_name(dest.name + ".old")
    for p in (new, old):
        if p.exists():
            shutil.rmtree(p)
    new.mkdir(parents=True)
    with tarfile.open(source, "r:xz") as t:
        t.extractall(new, filter="tar")
    if dest.exists():
        dest.rename(old)
    new.rename(dest)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    system.use_bundled_tools()
    on_line("STEP Decoders installed")
    return True


def install(parts, accept_mbelib: bool, on_line=lambda s: None) -> list[str]:
    """Install *parts*; returns those still missing afterwards. Runs for minutes: call from a thread.

    Lines starting "STEP " are progress for the user; everything goes to setup.log.
    """
    kinds = {PARTS[p][2] for p in parts}
    builds = kinds & {"build", "mbelib"}
    steps = []
    if "bundle" in kinds and not install_tools(on_line):
        return missing(parts)
    # DSD-FME's build needs compilers and -dev packages, which system.sh installs.
    if "system" in kinds or builds:
        steps.append(["pkexec", "bash", setup_script("system.sh")])
    if builds:
        # The bundle has the rest: only DSD-FME (mbelib's patent notice keeps it out of the bundle) is built here.
        steps.append(["bash", setup_script("build.sh"), "--only=dsd"] + (["--accept-mbelib"] if accept_mbelib else []))
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
