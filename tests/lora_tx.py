"""LoRa test transmitter (gr-lora_sdr's TX chain): payloads in, complex64 IQ file out.

    python3 tests/lora_tx.py OUT.c64 --rate 1000000 --bw 250000 --sf 11 --cr 1 --sync 0x2B \\
        --preamble 16 HEXPAYLOAD [HEXPAYLOAD ...]

Run with gr-lora_sdr on PYTHONPATH / LD_LIBRARY_PATH (see sdr_toolkit.modes.lora.lora_available).
"""

import argparse
import time

import pmt
from gnuradio import blocks, gr
import gnuradio.lora_sdr as lora_sdr


def main():
    p = argparse.ArgumentParser()
    p.add_argument("out")
    p.add_argument("payloads", nargs="+")
    p.add_argument("--rate", type=int, default=1000000)
    p.add_argument("--bw", type=int, default=250000)
    p.add_argument("--sf", type=int, default=11)
    p.add_argument("--cr", type=int, default=1)
    p.add_argument("--sync", default="0x2B")
    p.add_argument("--preamble", type=int, default=16)
    a = p.parse_args()

    tb = gr.top_block()
    white = lora_sdr.whitening(True, False, ",", "packet_len")
    head = lora_sdr.header(False, True, a.cr)
    crc = lora_sdr.add_crc(True)
    ham = lora_sdr.hamming_enc(a.cr, a.sf)
    inter = lora_sdr.interleaver(a.cr, a.sf, 2, a.bw)
    gray = lora_sdr.gray_demap(a.sf)
    mod = lora_sdr.modulate(a.sf, a.rate, a.bw, [int(a.sync, 0)], 2**10, a.preamble)
    sink = blocks.file_sink(gr.sizeof_gr_complex, a.out)
    sink.set_unbuffered(True)
    tb.connect(white, head, crc, ham, inter, gray, mod, sink)
    for x in a.payloads:
        white.to_basic_block()._post(pmt.intern("msg"), pmt.intern(x))
    # The modulator waits for more frames once these are sent: stop when output stops growing.
    tb.start()
    last, still = -1, 0
    while still < 10:
        time.sleep(0.1)
        n = mod.nitems_written(0)
        still = still + 1 if n == last and n > 0 else 0
        last = n
    tb.stop()
    tb.wait()


if __name__ == "__main__":
    main()
