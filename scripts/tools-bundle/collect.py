"""Collects the decoders into one relocatable folder (run in the build container by inside.sh):

    stage/bin/NAME        a wrapper: sets the bundle's libraries (and Python modules) for this program only
    stage/libexec/NAME    the program
    stage/lib/            every library they need that a desktop Ubuntu doesn't have anyway
    stage/python/         GNU Radio's, gr-satellites' and gr-lora_sdr's Python modules
    stage/satdump/        SatDump in its portable layout (plugins/, resources/, pipelines/ beside it)

Libraries are found with ldd, recursively; the base system (glibc, libstdc++, graphics, audio, udev, D-Bus) is
left out, so the programs use this PC's. Built-in library paths (RPATH/RUNPATH) are removed: the wrappers'
LD_LIBRARY_PATH decides.

    python3 collect.py PARTS_DIR STAGE_DIR
"""

import glob
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

PROGRAMS = ["rtl_433", "readsb", "direwolf", "jt9", "multimon-ng", "rtl_sdr", "rtl_fm", "rtl_tcp", "rtl_test",
            "rtl_power", "gnss-sdr", "welle-cli", "wmbusmeters", "rtl_wmbus", "odr-dabmod", "odr-dabmux",
            "odr-audioenc", "odr-padenc", "ffmpeg",
            # test-signal generators the tests use (direwolf's and WSJT-X's)
            "gen_packets", "atest", "ft8sim", "ft4code", "ft8code"]
PART_PROGRAMS = ["aero/bin/acarsdec", "aero/bin/dumpvdl2", "aero/bin/dumphfdl", "rtlamr/bin/rtlamr",
                 "radiosonde/bin/*"]
EXTRA_LIBS = ["libhackrf.so.0", "librtlsdr.so.0"]       # loaded by the built-in relay through ctypes
PYTHON_PROGRAMS = ["gr_satellites"]
PY_ROOTS = ["gnuradio", "pmt", "satellites"]            # and their python3-* dependencies (below)
# The base system: every Ubuntu desktop has these, and the PC's own must be used (its graphics driver, its
# audio server, its udev).
BASE = re.compile(r"^(ld-linux.*|libc\.so.*|libm\.so.*|libdl\.so.*|libpthread\.so.*|librt\.so.*|libresolv\.so.*|"
                  r"libutil\.so.*|libstdc\+\+\.so.*|libgcc_s\.so.*|libGL.*|libEGL.*|libGLX.*|libGLdispatch.*|"
                  r"libOpenGL.*|libvulkan.*|libdrm.*|libgbm.*|libX.*|libxcb.*|libxkb.*|libwayland.*|libasound\.so.*|"
                  r"libpulse.*|libpipewire.*|libjack.*|libudev\.so.*|libsystemd\.so.*|libdbus-1\.so.*|libselinux.*|"
                  r"libcap\.so.*|libmvec\.so.*)$")
WRAPPER = """#!/bin/sh
# SDR Toolkit's bundled {name}: its libraries from the bundle, for this program only.
d=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
export LD_LIBRARY_PATH="$d/lib${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
{extra}exec {run} "$@"
"""


def deps(path: Path) -> list[Path]:
    out = subprocess.run(["ldd", str(path)], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        m = re.match(r"\s*(\S+) => (/\S+)", line)
        if m and not BASE.match(m.group(1)):
            found.append(Path(m.group(2)))
        elif "not found" in line:
            print("  MISSING", path, line.strip())
    return found


def copy_libs(start: list[Path], lib: Path):
    todo, seen = list(start), set()
    while todo:
        p = todo.pop()
        for d in deps(p):
            real = d.resolve()
            if d.name in seen:
                continue
            seen.add(d.name)
            dst = lib / d.name                                    # under its soname, as the loader asks
            if not dst.exists():
                shutil.copy2(real, dst)
                os.chmod(dst, 0o755)
                strip_rpath(dst)
            todo.append(real)
    return seen


def strip_rpath(p: Path):
    subprocess.run(["patchelf", "--remove-rpath", str(p)], capture_output=True)


def wrapper(stage: Path, name: str, run: str, extra=""):
    w = stage / "bin" / name
    w.write_text(WRAPPER.format(name=name, run=run, extra=extra))
    w.chmod(w.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def python_deps(roots_pkgs: list[str]) -> set[str]:
    """The python3-* packages the given Debian packages need, recursively."""
    out = subprocess.run(["apt-cache", "depends", "--recurse", "--no-recommends", "--no-suggests", "--no-conflicts",
                          "--no-breaks", "--no-replaces", "--no-enhances", *roots_pkgs], capture_output=True,
                         text=True).stdout
    pk = {m for m in re.findall(r"^\s*(?:Depends: )?<?(python3-[a-z0-9.+-]+)>?", out, re.M)}
    return {p for p in pk if p not in ("python3-numpy", "python3-minimal", "python3-dev")}


def main():
    parts, stage = Path(sys.argv[1]), Path(sys.argv[2])
    if stage.exists():
        shutil.rmtree(stage)
    for sub in ("bin", "libexec", "lib", "python"):
        (stage / sub).mkdir(parents=True)
    lib = stage / "lib"
    progs = []
    for name in PROGRAMS:
        p = shutil.which(name)
        if p is None:
            print("  NOT FOUND", name)
            continue
        progs.append((name, Path(p)))
    for pattern in PART_PROGRAMS:
        for p in sorted(glob.glob(str(parts / pattern))):
            progs.append((Path(p).name, Path(p)))
    elf = []
    for name, p in progs:
        dst = stage / "libexec" / name
        shutil.copy2(p.resolve(), dst)
        strip_rpath(dst)
        elf.append(dst)
        wrapper(stage, name, f'"$d/libexec/{name}"')
    # Libraries the parts built themselves (libacars) sit in their lib folders: copied first, found by name.
    built = glob.glob(str(parts / "*/lib/*.so*")) + glob.glob(str(parts / "*/lib/*/*.so*"))
    for p in built:
        if not Path(p).is_symlink():
            dst = lib / Path(p).name
            shutil.copy2(p, dst)
            strip_rpath(dst)
    for p in built:
        if Path(p).is_symlink():
            target = os.readlink(p)
            link = lib / Path(p).name
            if not link.exists():
                link.symlink_to(Path(target).name)
    os.environ["LD_LIBRARY_PATH"] = ":".join(glob.glob(str(parts / "*/lib")) + glob.glob(str(parts / "*/lib/*-linux-gnu")))
    for name in EXTRA_LIBS:
        found = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
        m = re.search(rf"{re.escape(name)} .*=> (\S+)", found)
        if m:
            shutil.copy2(Path(m.group(1)).resolve(), lib / name)
            elf.append(lib / name)
    # SatDump, portable: the program, its plugins and resources together, run from its own folder.
    sd = stage / "satdump"
    sd.mkdir()
    shutil.copy2(Path(shutil.which("satdump")).resolve(), sd / "satdump")
    strip_rpath(sd / "satdump")
    shutil.copytree("/usr/lib/x86_64-linux-gnu/satdump/plugins", sd / "plugins")
    for item in Path("/usr/share/satdump").iterdir():
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, sd / item.name)
    for so in ("libsatdump_core.so", "libsatdump_interface.so"):
        shutil.copy2(f"/usr/lib/x86_64-linux-gnu/{so}", lib / so)
        strip_rpath(lib / so)
    elf += [sd / "satdump", *sorted((sd / "plugins").glob("*.so"))]
    for p in (sd / "plugins").glob("*.so"):
        strip_rpath(p)
    wrapper(stage, "satdump", "./satdump", extra='cd "$d/satdump" || exit 1\n')
    # Python: GNU Radio, gr-satellites (and what they need), and gr-lora_sdr's module.
    pys = Path("/usr/lib/python3/dist-packages")
    pkgs = python_deps(["gnuradio", "gr-satellites"]) | {"gr-satellites"}
    files = subprocess.run(["dpkg", "-L", *sorted(pkgs)], capture_output=True, text=True).stdout.splitlines()
    for f in files:
        fp = Path(f)
        if str(fp).startswith(str(pys) + "/") and fp.is_file():
            dst = stage / "python" / fp.relative_to(pys)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, dst)
    for f in glob.glob(str(parts / "gr-lora_sdr/lib*/python3*/*-packages/gnuradio/lora_sdr")) + \
            glob.glob(str(parts / "gr-lora_sdr/lib/*/python3*/*-packages/gnuradio/lora_sdr")):
        shutil.copytree(f, stage / "python/gnuradio/lora_sdr", dirs_exist_ok=True)
    py_so = [Path(p) for p in glob.glob(str(stage / "python/**/*.so"), recursive=True)]
    for p in py_so:
        strip_rpath(p)
    for name in PYTHON_PROGRAMS:
        shutil.copy2(shutil.which(name), stage / "libexec" / name)
        wrapper(stage, name, f'python3 "$d/libexec/{name}"',
                extra='export PYTHONPATH="$d/python${PYTHONPATH:+:$PYTHONPATH}"\n')
    names = copy_libs(elf + py_so, lib)
    print(f"== {len(progs) + 2} programs, {len(names)} libraries")
    # Check: every program and module resolves all its libraries from the bundle and the base system.
    env = dict(os.environ, LD_LIBRARY_PATH=str(lib))
    bad = 0
    for p in elf + py_so + sorted(lib.glob("*.so*")):
        out = subprocess.run(["ldd", str(p)], capture_output=True, text=True, env=env).stdout
        if "not found" in out:
            bad += 1
            print("  UNRESOLVED", p, [ln.strip() for ln in out.splitlines() if "not found" in ln][:3])
    print(f"== unresolved: {bad}")
    (stage / "VERSION").write_text(subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True,
                                                  text=True).stdout)


if __name__ == "__main__":
    main()
