"""sdr-toolkit-helper.exe, the Windows build's stand-in for ``python -m``: runs one of the app's
helper modules (relay_stream, the satellite worker) as its own process, with real stdin and stdout
(the window's .exe has none). Usage: sdr-toolkit-helper.exe MODULE [ARGS...]"""

import runpy
import sys

ALLOWED = {"sdr_toolkit.relay_stream", "sdr_toolkit.satellites.worker", "sdr_toolkit.engines.lora_rx",
           "sdr_toolkit.engines.ism_wide", "sdr_toolkit.engines.sonde_rx", "sdr_toolkit.engines.navaid",
           "sdr_toolkit.engines.dsc", "sdr_toolkit.engines.beacon406", "sdr_toolkit.engines.lora_find",
           "sdr_toolkit.engines.fm_band",
           "sdr_toolkit.engines.dab", "sdr_toolkit.engines.satdump_run",
           "sdr_toolkit.engines.astro"}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ALLOWED:
        sys.exit(f"usage: {sys.argv[0]} MODULE [ARGS...], MODULE one of {', '.join(sorted(ALLOWED))}")
    module = sys.argv[1]
    sys.argv = [module] + sys.argv[2:]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
