"""Tests that need an outside program (jt9, direwolf, rtl_433, readsb...) which isn't on this machine
are reported as skipped, saying which, rather than as failures: scripts/install.sh sets them all up
on Linux, and some don't exist for Windows at all. Only these cases are turned into skips:

- the mode raised MissingParts (the app's own "not installed" check), or
- a FileNotFoundError names a program that isn't on PATH here.

SDR_TOOLKIT_ALL_TOOLS=1 turns that off, so a fully set-up PC can make sure nothing is skipped.
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

# From a source checkout (CI installs the package; here the tests run from src/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdr_toolkit.modes.base import MissingParts  # noqa: E402


def _program_from_traceback(excinfo) -> str | None:
    """The program a failed subprocess start was for. Windows' FileNotFoundError doesn't name it:
    it's in _execute_child's arguments (a command line string there, a list on Linux)."""
    for entry in reversed(excinfo.traceback):
        frame = entry.frame
        if frame.code.name != "_execute_child":
            continue
        f_locals = frame.f_locals
        exe = f_locals.get("executable")
        if exe:
            return str(exe)
        args = f_locals.get("args")
        if isinstance(args, (list, tuple)) and args:
            return str(args[0])
        if isinstance(args, str) and args:
            return args[1:].split('"', 1)[0] if args.startswith('"') else args.split(" ", 1)[0]
    return None


def _missing_program(excinfo) -> str | None:
    exc = excinfo.value
    if not isinstance(exc, FileNotFoundError):
        return None
    path = exc.filename or _program_from_traceback(excinfo)
    if not path:
        return None
    name = Path(str(path)).name
    return name if shutil.which(name) is None and not Path(str(path)).is_file() else None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("SDR_TOOLKIT_ALL_TOOLS") == "1" or not report.failed or call.excinfo is None:
        return
    exc = call.excinfo.value
    if isinstance(exc, MissingParts):
        reason = f"needs {', '.join(exc.parts)}, not installed here"
    elif (prog := _missing_program(call.excinfo)) is not None:
        reason = f"needs {prog}, not installed here"
    else:
        return
    report.outcome = "skipped"
    report.longrepr = (str(item.path), item.location[1] or 0, f"Skipped: {reason}")
