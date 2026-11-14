"""LoRaWAN frames: what can be read without the network's keys.

The PHYPayload (LoRaWAN 1.0 / 1.1) is a header byte (MHDR: message type and version), then:
  join request:  JoinEUI (8 bytes) DevEUI (8) DevNonce (2) MIC (4), all in the clear;
  join accept:   encrypted (with the device's AppKey), 12 or 28 bytes and a MIC;
  data frames:   DevAddr (4) FCtrl (1) FCnt (2) FOpts (0-15) [FPort (1) FRMPayload] MIC (4); the
                 payload is encrypted with session keys, the rest is readable.
Multi-byte fields are little-endian; EUIs and addresses are shown most significant byte first, as
consoles show them.
"""

from dataclasses import dataclass, field

MTYPES = {0: "join request", 1: "join accept", 2: "unconfirmed data up", 3: "unconfirmed data down",
          4: "confirmed data up", 5: "confirmed data down", 6: "rejoin request", 7: "proprietary"}
# Networks recognisable by their DevAddr's top bits (NetID type 0, 7-bit prefix).
NETWORKS = {0x13: "The Things Network"}           # NetID 0x000013: DevAddr 26xxxxxx / 27xxxxxx


@dataclass
class Frame:
    mtype: int
    kind: str
    uplink: bool
    fields: dict = field(default_factory=dict)


def _le(b: bytes) -> str:
    return b[::-1].hex().upper()


def parse(b: bytes) -> Frame | None:
    """A LoRaWAN frame, or None if b can't be one (wrong version, or too short for its type)."""
    if len(b) < 5:
        return None
    mtype, major = b[0] >> 5, b[0] & 0x03
    if major != 0:
        return None
    up = mtype in (0, 2, 4, 6)
    f = Frame(mtype, MTYPES[mtype], up)
    if mtype == 0:
        if len(b) != 23:
            return None
        f.fields = {"join_eui": _le(b[1:9]), "dev_eui": _le(b[9:17]), "dev_nonce": int.from_bytes(b[17:19], "little")}
    elif mtype == 1:
        if len(b) not in (17, 33):
            return None
    elif mtype in (2, 3, 4, 5):
        if len(b) < 12:
            return None
        fctrl = b[5]
        fopts = fctrl & 0x0F
        end = len(b) - 4                                   # the MIC
        if 8 + fopts > end:
            return None
        addr = int.from_bytes(b[1:5], "little")
        f.fields = {"dev_addr": f"{addr:08X}", "fcnt": int.from_bytes(b[6:8], "little"),
                    "adr": bool(fctrl & 0x80), "ack": bool(fctrl & 0x20), "fopts": fopts}
        net = NETWORKS.get(addr >> 25)
        if net:
            f.fields["network"] = net
        i = 8 + fopts
        if i < end:
            f.fields["fport"] = b[i]
            f.fields["payload_bytes"] = end - i - 1
        else:
            f.fields["payload_bytes"] = 0
    return f


def describe(f: Frame) -> str:
    x = f.fields
    if f.mtype == 0:
        return f"LoRaWAN join request from device {x['dev_eui']} (join server {x['join_eui']})"
    if f.mtype == 1:
        return "LoRaWAN join accept (encrypted: for the device that asked)"
    if f.mtype in (2, 3, 4, 5):
        way = "uplink" if f.uplink else "downlink"
        conf = "confirmed " if f.mtype in (4, 5) else ""
        who = f"device {x['dev_addr']}" + (f" ({x['network']})" if x.get("network") else "")
        text = f"LoRaWAN {conf}{way} {'from' if f.uplink else 'to'} {who}, frame {x['fcnt']}"
        if "fport" in x:
            text += (f", port {x['fport']}, {x['payload_bytes']} bytes encrypted" if x["fport"] else
                     f", MAC commands ({x['payload_bytes']} bytes, encrypted)")
        elif x["fopts"]:
            text += f", {x['fopts']} bytes of MAC commands"
        if x.get("ack"):
            text += ", acknowledging"
        return text
    return f"LoRaWAN {f.kind}"
