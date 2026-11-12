"""Starts the real SDR Tools Relay (its source from SDR_TOOLS_RELAY_SRC) for RelayLiveTest:
its dongle plays a recording, and its LAN responder answers scans. Prints "READY" when it's up.

    python3 run_relay.py RECORDING TCP_PORT UDP_PORT PASSWORD
"""
import os
import sys
import time

sys.path.insert(0, os.environ["SDR_TOOLS_RELAY_SRC"])
from sdr_relay.discovery import Responder  # noqa: E402
from sdr_relay.rtlsdr import FileDongle  # noqa: E402
from sdr_relay.server import Config, Relay  # noqa: E402

recording, port, udp, password = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
relay = Relay(Config(name="Live test relay", port=port, password=password, file=recording), version="live")
relay.open_dongle = lambda: FileDongle(recording)
relay.start()
Responder(relay, udp).start()
print("READY", flush=True)
while sys.stdin.readline():
    pass
relay.stop()
