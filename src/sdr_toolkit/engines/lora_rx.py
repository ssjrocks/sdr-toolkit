"""LoRa receiver: unsigned 8-bit IQ in (a file, or stdin from rtl_sdr), frames out.

Runs as its own process (GNU Radio with gr-lora_sdr):

    python3 -m sdr_toolkit.engines.lora_rx --input FILE|- --rate 1000000 --bw 250000 --sf 11 \\
        --cr 1 --sync 0x2B --preamble 16 [--offset HZ]

gr-lora_sdr's CRC block prints each frame as "rx msg: 0x.., 0x.." followed by
"CRC valid!" or "CRC invalid"; the toolkit reads those lines.
"""

import argparse
import math
import signal
import sys

OVERSAMPLE = 4


def build(args):
    from gnuradio import blocks, filter, gr
    import gnuradio.lora_sdr as lora_sdr

    tb = gr.top_block("lora_rx")
    if args.input == "-":
        src = blocks.file_descriptor_source(gr.sizeof_char, 0, False)
    else:
        src = blocks.file_source(gr.sizeof_char, args.input, False)
    to_f = blocks.uchar_to_float()
    centre = blocks.add_const_ff(-127.5)
    scale = blocks.multiply_const_ff(1 / 127.5)
    split = blocks.deinterleave(gr.sizeof_float)
    iq = blocks.float_to_complex()
    tb.connect(src, to_f, centre, scale, split)
    tb.connect((split, 0), (iq, 0))
    tb.connect((split, 1), (iq, 1))
    last = iq
    if args.offset:
        rot = blocks.rotator_cc(-2 * math.pi * args.offset / args.rate)
        tb.connect(last, rot)
        last = rot
    out_rate = int(args.bw * OVERSAMPLE)
    if args.rate != out_rate:
        g = math.gcd(int(args.rate), out_rate)
        res = filter.rational_resampler_ccc(out_rate // g, int(args.rate) // g)
        tb.connect(last, res)
        last = res
    sync = [int(args.sync, 0)]
    fs = lora_sdr.frame_sync(int(args.centre), int(args.bw), args.sf, False, sync, OVERSAMPLE, args.preamble)
    demod = lora_sdr.fft_demod(False, True)
    gray = lora_sdr.gray_mapping(False)
    deint = lora_sdr.deinterleaver(False)
    ham = lora_sdr.hamming_dec(False)
    head = lora_sdr.header_decoder(False, args.cr, 255, True, 2, False)
    dew = lora_sdr.dewhitening()
    crc = lora_sdr.crc_verif(2, False)          # 2: print frames as hex
    sink = blocks.null_sink(gr.sizeof_char)
    # At SF11-12 frame_sync wants more samples at once than GNU Radio's default buffers hold.
    big = 4 * (2**args.sf) * OVERSAMPLE
    for blk in (last, fs, demod):
        blk.set_min_output_buffer(big)
    tb.connect(last, fs, demod, gray, deint, ham, head, dew, crc, sink)
    tb.msg_connect((head, "frame_info"), (fs, "frame_info"))
    return tb


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--rate", type=float, required=True)
    p.add_argument("--bw", type=float, default=250000)
    p.add_argument("--sf", type=int, default=11)
    p.add_argument("--cr", type=int, default=1, help="1-4 for 4/5 to 4/8")
    p.add_argument("--sync", default="0x2B")
    p.add_argument("--preamble", type=int, default=8)
    p.add_argument("--offset", type=float, default=0.0)
    p.add_argument("--centre", type=float, default=868e6, help="for clock-drift correction")
    args = p.parse_args()
    try:
        tb = build(args)
    except ImportError as e:
        print(f"LORA_MISSING {e}", flush=True)
        sys.exit(3)
    signal.signal(signal.SIGTERM, lambda *a: (tb.stop(), sys.exit(0)))
    print("LORA_READY", flush=True)
    tb.run()


if __name__ == "__main__":
    main()
