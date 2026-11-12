"""Tests that need an outside program (jt9, direwolf, rtl_433, readsb...) which isn't on this machine
are reported as skipped, saying which, rather than as failures: scripts/install.sh sets them all up
on Linux, and some don't exist for Windows at all. Only these cases are turned into skips:

- the mode raised MissingParts (the app's own "not installed" check), or
- a FileNotFoundError names a program that isn't on PATH here.

SDR_TOOLKIT_ALL_TOOLS=1 turns that off, so a fully set-up PC can make sure nothing is skipped.
"""

import os
import shutil
from pathlib import Path

import pytest

from sdr_toolkit.modes.base import MissingParts


def _missing_program(exc) -> str | None:
    if not isinstance(exc, FileNotFoundError) or not exc.filename:
        return None
    name = Path(str(exc.filename)).name
    return name if shutil.which(name) is None and not Path(str(exc.filename)).is_file() else None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("SDR_TOOLKIT_ALL_TOOLS") == "1" or not report.failed or call.excinfo is None:
        return
    exc = call.excinfo.value
    if isinstance(exc, MissingParts):
        reason = f"needs {', '.join(exc.parts)}, not installed here"
    elif (prog := _missing_program(exc)) is not None:
        reason = f"needs {prog}, not installed here"
    else:
        return
    report.outcome = "skipped"
    report.longrepr = (str(item.path), item.location[1] or 0, f"Skipped: {reason}")
