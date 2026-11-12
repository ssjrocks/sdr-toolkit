"""What differs between Linux and Windows: where files go, how helper processes start, and where
the outside programs are.

Linux keeps the XDG places (~/.local/state, ~/.local/share, ~/.cache, each with sdr-toolkit/).
Windows keeps everything in %LOCALAPPDATA%\\SDR Toolkit. The Windows build is frozen (PyInstaller):
there's no python.exe to run helper modules with, so it ships a console helper,
sdr-toolkit-helper.exe, that runs a module given by name (see helper.py).
"""

import os
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
FROZEN = bool(getattr(sys, "frozen", False))
HELPER = "sdr-toolkit-helper.exe" if WINDOWS else "sdr-toolkit-helper"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default) / "sdr-toolkit"


def _windows(sub: str) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SDR Toolkit"
    return base / sub if sub else base


def _made(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir() -> Path:
    """Logs, update state, setup logs."""
    return _made(_windows("State") if WINDOWS else _xdg("XDG_STATE_HOME", ".local/state"))


def data_dir() -> Path:
    """The logbook, the aircraft database, parts built or added for the app."""
    return _made(_windows("") if WINDOWS else _xdg("XDG_DATA_HOME", ".local/share"))


def cache_dir() -> Path:
    """Map tiles and satellite data: safe to delete."""
    return _made(_windows("Cache") if WINDOWS else _xdg("XDG_CACHE_HOME", ".cache"))


def bundle_dir() -> Path | None:
    """Where a frozen build keeps its files (PyInstaller's _internal), else None."""
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) if FROZEN else None


def tool_dirs() -> list[Path]:
    """Folders searched for outside programs before PATH: those bundled with the Windows build, a
    folder for programs the user adds (%LOCALAPPDATA%\\SDR Toolkit\\tools), and WSJT-X's usual
    install folders (for jt9)."""
    if not WINDOWS:
        return []
    dirs = []
    if bundle_dir():
        dirs.append(bundle_dir() / "tools")
    dirs.append(user_tools_dir())
    for base in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), "C:\\WSJT")):
        dirs += [Path(base) / "wsjtx" / "bin", Path(base) / "WSJT-X" / "bin"]
    return dirs


def user_tools_dir() -> Path:
    return _made(data_dir() / "tools")


def setup_environment() -> None:
    """Once, at start: outside programs found by name (PATH), and no console window flashing up
    for each one the Windows build starts."""
    if not WINDOWS:
        return
    extra = [str(d) for d in tool_dirs() if d.is_dir()]
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
    if getattr(subprocess.Popen, "_sdr_no_console", False):
        return

    class Popen(subprocess.Popen):
        _sdr_no_console = True

        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    # subprocess.run and check_output look Popen up in the module at call time.
    subprocess.Popen = Popen


def python_command(module: str, *args: str) -> list[str]:
    """The command line that runs a module of this package as its own process (unbuffered),
    from a checkout, an install, or the Windows build."""
    if FROZEN:
        return [str(Path(sys.executable).with_name(HELPER)), module, *args]
    pkg_parent = str(Path(__file__).resolve().parents[1])         # works installed or from a checkout
    boot = (f"import sys, runpy; sys.path.insert(0, {pkg_parent!r}); "
            f"runpy.run_module({module!r}, run_name='__main__', alter_sys=True)")
    return [sys.executable, "-u", "-c", boot, *args]


# On Windows a dongle needs the WinUSB driver before anything can open it.
DRIVER_HINT = (" On Windows the dongle needs the WinUSB driver: install it with Zadig (zadig.akeo.ie), or with "
               "SDR Tools Relay's installer.") if WINDOWS else ""


def open_folder(path: Path) -> None:
    """Shows a folder in the file manager."""
    if WINDOWS:
        os.startfile(str(path))                 # noqa: S606 - opening a folder of ours
    else:
        subprocess.Popen(["xdg-open", str(path)])
