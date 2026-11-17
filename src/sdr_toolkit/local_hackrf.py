"""A HackRF plugged into this PC. The toolkit's SDR modes drive an RTL-SDR here with its own tools
(rtl_sdr, rtl_tcp, readsb...), which know nothing of a HackRF; SDR Tools Relay does. So a local HackRF
is served by the relay's own server (hackrf_local, copied from the relay unchanged), listening on
127.0.0.1 only with a password of its own, and used through the same bridge as a relay on the network:
every mode, the HackRF bar and its automatic gain, the sweep and the link meters work the same.

Whether one is plugged in is read from the USB device list (on Linux /sys, so nothing touches the
device); it's opened only when a mode streams from it."""

import logging
import secrets
import sys
from pathlib import Path

log = logging.getLogger(__name__)

NAME = "This PC's HackRF"
USB_IDS = {("1d50", "6089"), ("1d50", "604b"), ("1d50", "cc15")}   # HackRF One, Jawbreaker, rad1o


def plugged_in() -> bool:
    if sys.platform.startswith("linux"):
        for d in Path("/sys/bus/usb/devices").glob("*"):
            try:
                ids = ((d / "idVendor").read_text().strip(), (d / "idProduct").read_text().strip())
            except OSError:
                continue
            if ids in USB_IDS:
                return True
        return False
    try:
        from .hackrf_local.hackrf import devices
        return bool(devices())
    except Exception:  # no libhackrf, or no driver: none to use
        return False


class LocalHackRF:
    """The built-in relay for this PC's HackRF."""

    def __init__(self):
        self.relay = None
        self.password = secrets.token_hex(16)

    @property
    def port(self) -> int:
        return self.relay.config.port if self.relay is not None else 0

    def start(self):
        """Raises RuntimeError (with what's wrong) if the HackRF can't be found through libhackrf."""
        from .hackrf_local import RELAY_VERSION
        from .hackrf_local.rtlsdr import RtlError
        from .hackrf_local.server import Config, Relay
        try:
            from .hackrf_local.hackrf import devices
            found = devices()
        except RtlError as e:
            raise RuntimeError(f"The HackRF is plugged in, but libhackrf can't use it: {e}") from None
        if not found:
            raise RuntimeError("The HackRF is plugged in, but libhackrf doesn't list it (its USB permissions?)")
        device = f"hackrf:{found[0]['serial']}"
        self.relay = Relay(Config(name=NAME, host="127.0.0.1", port=0, password=self.password, device=device),
                           version=RELAY_VERSION)
        self.relay.start()
        log.info("built-in relay for %s on 127.0.0.1:%d", device, self.port)
        return self

    def stop(self):
        if self.relay is not None:
            self.relay.stop()
            self.relay = None
