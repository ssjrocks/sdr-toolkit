"""An rtl_tcp client that writes the samples to stdout, like rtl_sdr or rtl_fm do for a local dongle.

    python -m sdr_toolkit.relay_stream --port BRIDGE_PORT --freq HZ --rate HZ [--gain DB]
        [--format cu8|s8] [--fm AUDIO_RATE --channel HZ --offset HZ]

It connects to the window's relay bridge (127.0.0.1), which does the relay's handshake.
--format s8 gives signed bytes (GNSS-SDR); --fm demodulates narrow FM to 16-bit audio
the way the toolkit does for recordings (AIS).
"""

import argparse
import socket
import struct
import sys

import numpy as np

SET_FREQ, SET_RATE, SET_GAIN_MODE, SET_GAIN = 1, 2, 3, 4


def command(cmd, value) -> bytes:
    return struct.pack(">BI", cmd, int(value) & 0xFFFFFFFF)


QUIET = 10.0          # s without samples before giving up (the relay's dongle isn't streaming)


def chunks(sock, size):
    buf = b""
    sock.settimeout(QUIET)
    while True:
        try:
            data = sock.recv(size)
        except TimeoutError:
            sys.exit(f"relay_stream: the relay sent no samples for {QUIET:.0f} s: its dongle isn't streaming "
                     "(see the relay's window, or Check the relay's dongle in the relay menu)")
        if not data:
            return
        buf += data
        if len(buf) >= size:
            yield buf[: len(buf) // 2 * 2]
            buf = buf[len(buf) // 2 * 2:]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--freq", type=float, required=True)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--gain", default="auto")
    ap.add_argument("--format", choices=("cu8", "s8"), default="cu8")
    ap.add_argument("--fm", type=int, default=0, help="demodulate FM to this audio rate")
    ap.add_argument("--channel", type=float, default=25000)
    ap.add_argument("--offset", type=float, default=0, help="where the channel sits from the tuned frequency")
    a = ap.parse_args(argv)
    try:
        sock = socket.create_connection((a.host, a.port), timeout=10)
    except OSError as e:
        sys.exit(f"relay_stream: couldn't reach the relay bridge: {e}")
    cmds = command(SET_RATE, a.rate) + command(SET_FREQ, a.freq)
    if a.gain.lower() in ("", "auto"):
        cmds += command(SET_GAIN_MODE, 0)
    else:
        cmds += command(SET_GAIN_MODE, 1) + command(SET_GAIN, round(float(a.gain) * 10))
    try:
        sock.sendall(cmds)
        head = b""
        while len(head) < 12:
            data = sock.recv(12 - len(head))
            if not data:
                sys.exit("relay_stream: the relay closed the connection (busy, wrong password or no dongle: "
                         "see the relay's window)")
            head += data
        if head[:4] != b"RTL0":
            sys.exit("relay_stream: not an rtl_tcp stream")
        out = sys.stdout.buffer
        size = int(a.rate) // 10 * 2                # 0.1 s
        if a.fm:
            from .modes.radio import cu8_to_complex, fm_audio
            for audio in fm_audio((cu8_to_complex(c) for c in chunks(sock, size)), int(a.rate), a.offset,
                                  a.fm, a.channel):
                out.write(audio)
                out.flush()
        else:
            for c in chunks(sock, size):
                out.write((np.frombuffer(c, np.uint8) ^ 0x80).tobytes() if a.format == "s8" else c)
                out.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except OSError as e:
        sys.exit(f"relay_stream: {e}")


if __name__ == "__main__":
    main()
