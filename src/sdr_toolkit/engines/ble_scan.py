"""Bluetooth LE devices around: every advertisement this PC's Bluetooth hears, as JSON lines (through bleak:
BlueZ on Linux, WinRT on Windows).

    python -m sdr_toolkit.engines.ble_scan [--seconds S]

  {"event": "adv", "address", "name", "rssi", "tx_power", "manufacturer": {cid: hex}, "services": [...],
   "service_data": {uuid: hex}}

A device is written at most every 2 s unless what it says changes.
"""

import argparse
import asyncio
import json
import sys
import time


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


async def scan(seconds):
    from bleak import BleakScanner
    last: dict[str, tuple[float, str]] = {}

    def seen(device, adv):
        d = {"event": "adv", "address": device.address, "name": adv.local_name or device.name or "",
             "rssi": adv.rssi, "tx_power": adv.tx_power,
             "manufacturer": {str(k): bytes(v).hex() for k, v in (adv.manufacturer_data or {}).items()},
             "services": list(adv.service_uuids or []),
             "service_data": {str(k): bytes(v).hex() for k, v in (adv.service_data or {}).items()}}
        sig = json.dumps({k: d[k] for k in ("name", "manufacturer", "services", "service_data")}, sort_keys=True)
        t0, s0 = last.get(device.address, (0.0, ""))
        now = time.monotonic()
        if sig != s0 or now - t0 >= 2.0:
            last[device.address] = (now, sig)
            emit(d)

    try:
        scanner = BleakScanner(detection_callback=seen)
        await scanner.start()
    except Exception as e:                                       # no adapter, adapter off, no permission
        sys.exit(f"ble_scan: couldn't start a Bluetooth scan: {e}")
    emit({"event": "scanning"})
    try:
        end = time.monotonic() + seconds if seconds else None
        while end is None or time.monotonic() < end:
            await asyncio.sleep(0.5)
    finally:
        await scanner.stop()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0)
    a = ap.parse_args(argv)
    try:
        asyncio.run(scan(a.seconds))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
