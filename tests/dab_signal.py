"""A DAB+ ensemble as IQ, made with OpenDigitalRadio's encoder chain (odr-audioenc, odr-padenc, odr-dabmux,
odr-dabmod, all in Ubuntu): two stations with now-playing text (DLS) and a slideshow picture, so the DAB+
page can be tested end to end without a transmitter in range.

    make_ensemble(folder, seconds) -> path of an unsigned 8-bit IQ file at 2.048 MS/s
"""

import os
import shutil
import subprocess
import time
import wave
from pathlib import Path

import numpy as np

TOOLS = ("odr-audioenc", "odr-padenc", "odr-dabmux", "odr-dabmod")
ENSEMBLE = "TOOLKIT TEST"
STATIONS = (("Test Radio One", "Test One", 0x8D01, "Now playing: the first test station", 440.0),
            ("Test Radio Two", "Test Two", 0x8D02, "Now playing: the second test station", 660.0))


def available() -> bool:
    return all(shutil.which(t) for t in TOOLS)


def _wav(path, seconds, hz):
    t = np.arange(int(48000 * seconds)) / 48000
    x = 0.3 * np.sin(2 * np.pi * hz * t)
    pcm = (np.stack([x, x], axis=1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(pcm.tobytes())


def _slide(path, text):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (320, 240), (30, 60, 120))
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 300, 220), outline=(255, 255, 255), width=4)
    d.text((40, 110), text, fill=(255, 255, 0))
    img.save(path, "JPEG", quality=80)


def make_ensemble(folder: Path, seconds=40) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    subs, services, comps = [], [], []
    for i, (label, short, sid, dls, hz) in enumerate(STATIONS, 1):
        _wav(folder / f"a{i}.wav", seconds, hz)
        slides = folder / f"slides{i}"
        slides.mkdir(exist_ok=True)
        _slide(slides / "slide.jpg", label)
        (folder / f"dls{i}.txt").write_text(dls)
        sock = f"sdrtk-pad{i}-{os.getpid()}"        # a socket name the tools put in /tmp
        pad = subprocess.Popen(["odr-padenc", "-d", str(slides), "-t", str(folder / f"dls{i}.txt"), "-o", sock,
                                "-s", "0"], stdout=open(folder / f"padenc{i}.log", "w"), stderr=subprocess.STDOUT)
        time.sleep(0.5)
        try:
            subprocess.run(["odr-audioenc", "-i", str(folder / f"a{i}.wav"), "-b", "64", "-o",
                            str(folder / f"a{i}.dabp"), "-p", "58", "-P", sock],
                           check=True, stdout=open(folder / f"audioenc{i}.log", "w"), stderr=subprocess.STDOUT,
                           timeout=300)
        finally:
            pad.terminate()
            pad.wait(5)
        services.append(f'srv{i} {{ id {sid:#x} label "{label}" shortlabel "{short}" pty {9 + i} }}')
        subs.append(f'sub{i} {{ type dabplus inputfile "{folder / f"a{i}.dabp"}" bitrate 64 id {i} protection 3 }}')
        comps.append(f'comp{i} {{ service srv{i} subchannel sub{i} user-applications {{ userapp "slideshow" }} }}')
    eti = folder / "ensemble.eti"
    frames = int(seconds / 0.024) - 20
    (folder / "ensemble.mux").write_text(f"""
general {{ dabmode 1 nbframes {frames} syslog false tist false managementport 0 }}
remotecontrol {{ telnetport 0 }}
ensemble {{ id 0x4fff ecc 0xf0 local-time-offset auto label "{ENSEMBLE}" shortlabel "TOOLKIT" }}
services {{ {" ".join(services)} }}
subchannels {{ {" ".join(subs)} }}
components {{ {" ".join(comps)} }}
outputs {{ file "file://{eti}?type=raw" }}
""")
    subprocess.run(["odr-dabmux", str(folder / "ensemble.mux")], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=300)
    iq = folder / "ensemble.iq"
    subprocess.run(["odr-dabmod", str(eti), "-f", str(iq), "-F", "u8", "-r", "2048000"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    return iq
