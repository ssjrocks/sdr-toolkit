"""The tools bundle (Linux): installing it into the app's folder, and that what's installed is whole."""

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sdr_toolkit import deps, system  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the tools bundle is for Linux")


def _tarball(path, version, files):
    with tarfile.open(path, "w:xz") as t:
        for name, data, mode in files + [("./VERSION", version.encode(), 0o644)]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    return path


def test_install_tools_from_a_bundle(tmp_path, monkeypatch):
    tools = tmp_path / "data" / "tools"
    monkeypatch.setattr(system, "tools_dir", lambda: tools)
    monkeypatch.setattr(deps, "tools_info", lambda: {"version": "v2"})
    hello = b"#!/bin/sh\necho hello from the bundle\n"
    old = _tarball(tmp_path / "old.tar.xz", "v1", [("./bin/old-tool", hello, 0o755)])
    new = _tarball(tmp_path / "new.tar.xz", "v2", [("./bin/hello", hello, 0o755), ("./lib/libx.so.1", b"x", 0o644)])
    lines = []
    assert deps.install_tools(lines.append, source=str(old))
    assert (tools / "bin" / "old-tool").is_file() and not deps.tools_current()
    assert deps.install_tools(lines.append, source=str(new))
    assert not (tools / "bin" / "old-tool").exists(), "the old bundle is replaced, not merged"
    assert deps.tools_current() and "STEP Decoders installed" in lines
    out = subprocess.run([str(tools / "bin" / "hello")], capture_output=True, text=True).stdout
    assert out.strip() == "hello from the bundle"
    assert sorted(p.name for p in tools.parent.iterdir()) == ["tools"]     # nothing left beside it
    # A bundle part counts as missing while the bundle isn't the one this release names.
    monkeypatch.setattr(deps, "tools_info", lambda: {"version": "v3"})
    assert "rtl-433" in deps.missing(["rtl-433"])


REAL = system.data_dir() / "tools"


@pytest.mark.skipif(not (REAL / "bin").is_dir(), reason="no tools bundle installed here")
def test_installed_bundle_is_whole():
    env = dict(os.environ, LD_LIBRARY_PATH=str(REAL / "lib"))
    files = [p for p in (REAL / "libexec").iterdir()] + [REAL / "satdump" / "satdump"] + \
        sorted((REAL / "satdump" / "plugins").glob("*.so")) + sorted((REAL / "python").rglob("*.so"))
    missing = {}
    for p in files:
        out = subprocess.run(["ldd", str(p)], capture_output=True, text=True, env=env).stdout
        if "not a dynamic executable" in out:
            continue
        lost = [ln.split()[0] for ln in out.splitlines() if "not found" in ln]
        if lost:
            missing[p.name] = lost
    assert not missing, missing
    # Every wrapper has its program.
    for w in (REAL / "bin").iterdir():
        text = w.read_text()
        assert w.name in text and os.access(w, os.X_OK)


@pytest.mark.skipif(not (REAL / "bin").is_dir(), reason="no tools bundle installed here")
@pytest.mark.parametrize("cmd, expect", [
    (["rtl_433", "-V"], "rtl_433"), (["readsb", "--version"], "readsb"), (["multimon-ng", "-h"], "multimon"),
    (["welle-cli", "-v"], "welle"), (["wmbusmeters", "--version"], "wmbusmeters"), (["ffmpeg", "-version"], "ffmpeg"),
    (["dumpvdl2", "--version"], "dumpvdl2"), (["dumphfdl", "--version"], "dumphfdl"),
    (["gnss-sdr", "--version"], "gnss-sdr"), (["gr_satellites", "--version"], "gr_satellites"),
    (["satdump", "version"], "SatDump"), (["direwolf", "-h"], "Dire Wolf"), (["acarsdec", "-V"], "Acarsdec"),
])
def test_bundled_programs_start(cmd, expect):
    p = subprocess.run([str(REAL / "bin" / cmd[0]), *cmd[1:]], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    out = p.stdout + p.stderr
    assert expect.lower() in out.lower(), (cmd, p.returncode, out[-500:])
