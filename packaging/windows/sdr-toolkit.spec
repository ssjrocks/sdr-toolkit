# PyInstaller spec for the Windows build (run from the repository's root by
# .github/workflows/windows.yml: pyinstaller --noconfirm packaging/windows/sdr-toolkit.spec).
#
# Two programs in one folder, sharing its libraries:
#   SDR Toolkit.exe          the window (no console)
#   sdr-toolkit-helper.exe   runs the helper modules the radio modes start (relay_stream, the
#                            satellite worker) with real stdin/stdout; see sdr_toolkit/helper.py
# build/tools (the RTL-SDR tools, put there by the workflow) is copied to _internal/tools, where
# sdr_toolkit.system looks for outside programs.

import os

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
SRC = os.path.join(ROOT, "src")
ICON = os.path.join(ROOT, "build", "icon.ico")
TOOLS = os.path.join(ROOT, "build", "tools")

# Every mode and engine (the window imports most of them dynamically); nothing that needs GNU Radio.
hidden = [m for m in collect_submodules("sdr_toolkit") if "lora_rx" not in m]
datas = copy_metadata("sdr-toolkit")                       # importlib.metadata.version("sdr-toolkit")
datas += [(os.path.join(TOOLS, f), "tools") for f in sorted(os.listdir(TOOLS))] if os.path.isdir(TOOLS) else []
excludes = ["gnuradio", "tkinter", "pytest", "IPython", "matplotlib"]

gui = Analysis([os.path.join(SPECPATH, "gui_entry.py")], pathex=[SRC], datas=datas, hiddenimports=hidden,
               excludes=excludes, noarchive=False)
helper = Analysis([os.path.join(SPECPATH, "helper_entry.py")], pathex=[SRC], hiddenimports=[
    "sdr_toolkit.relay_stream", "sdr_toolkit.satellites.worker", "sdr_toolkit.modes.radio"],
    excludes=excludes + ["PyQt6"], noarchive=False)

gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="SDR Toolkit", icon=ICON,
              console=False, upx=False)
helper_exe = EXE(PYZ(helper.pure), helper.scripts, [], exclude_binaries=True, name="sdr-toolkit-helper", icon=ICON,
                 console=True, upx=False)
COLLECT(gui_exe, gui.binaries, gui.datas, helper_exe, helper.binaries, helper.datas, name="SDR Toolkit", upx=False)
