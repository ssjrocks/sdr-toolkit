"""802.11 frames with radiotap headers in a pcap, for the Wi-Fi page's tests: beacons (WPA2, WPA3, open), a probe
request, and data frames between a device and its network."""

import struct


def _mac(s):
    return bytes(int(x, 16) for x in s.split(":"))


def radiotap(freq, signal):
    # present: channel (bit 3) and antenna signal in dBm (bit 5)
    return struct.pack("<BBHI", 0, 0, 13, (1 << 3) | (1 << 5)) + struct.pack("<HHb", freq, 0x00A0, signal)


def _ie(n, body):
    return bytes([n, len(body)]) + body


def rsn(akm):
    return _ie(48, struct.pack("<H", 1) + b"\x00\x0f\xac\x04" + struct.pack("<H", 1) + b"\x00\x0f\xac\x04"
               + struct.pack("<H", 1) + b"\x00\x0f\xac" + bytes([akm]) + b"\x00\x00")


def beacon(bssid, ssid, channel, akm=None):
    cap = 0x0011 if akm else 0x0001
    body = bytes(8) + struct.pack("<HH", 100, cap) + _ie(0, ssid.encode()) + _ie(1, b"\x82\x84\x8b\x96") \
        + _ie(3, bytes([channel])) + (rsn(akm) if akm else b"")
    return b"\x80\x00\x00\x00" + _mac("ff:ff:ff:ff:ff:ff") + _mac(bssid) + _mac(bssid) + b"\x00\x00" + body


def probe_request(client, ssid):
    body = _ie(0, ssid.encode()) + _ie(1, b"\x82\x84\x8b\x96")
    return b"\x40\x00\x00\x00" + _mac("ff:ff:ff:ff:ff:ff") + _mac(client) + _mac("ff:ff:ff:ff:ff:ff") + b"\x00\x00" + body


def data_to_ap(client, bssid, dst):
    # ToDS, protected (its payload encrypted: only who and where shows)
    return b"\x08\x41\x00\x00" + _mac(bssid) + _mac(client) + _mac(dst) + b"\x10\x00" + bytes(40)


def pcap(frames, path):
    """frames: [(time, freq MHz, signal dBm, 802.11 bytes)]"""
    out = [struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 127)]
    for t, freq, sig, f in frames:
        pkt = radiotap(freq, sig) + f
        out.append(struct.pack("<IIII", int(t), int((t % 1) * 1e6), len(pkt), len(pkt)) + pkt)
    with open(path, "wb") as fh:
        fh.write(b"".join(out))
    return path


def sample(path):
    t0 = 1_790_000_000.0
    fr = [(t0, 2437, -45, beacon("aa:bb:cc:00:00:01", "HomeNet", 6, akm=2)),
          (t0 + 0.1, 2412, -70, beacon("aa:bb:cc:00:00:02", "Cafe Free WiFi", 1)),
          (t0 + 0.2, 5180, -60, beacon("aa:bb:cc:00:00:03", "Neighbour", 36, akm=8)),
          (t0 + 0.3, 2437, -50, probe_request("12:34:56:78:9a:bc", "OldNetwork")),
          (t0 + 0.4, 2437, -48, data_to_ap("12:34:56:78:9a:bc", "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:01")),
          (t0 + 6.0, 2437, -46, beacon("aa:bb:cc:00:00:01", "HomeNet", 6, akm=2))]
    return pcap(fr, path)
