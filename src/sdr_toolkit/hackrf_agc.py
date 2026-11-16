"""Automatic gain for a HackRF, which has none of its own: once a second, from the input level the relay
measured over the whole band (or this PC, for an older relay), the LNA and VGA are stepped to keep the
ADC well used but not clipping.

- Clipping (more than 0.2% of samples at the limit): 8 dB less at once.
- Too hot (average above -14 dBFS) or now and then clipping (0.02%): 4 dB less.
- Too quiet (below -26 dBFS): up towards -20 dBFS, 6 dB a step (10 when far below), to 76 dB in all:
  past that a HackRF mostly amplifies its own noise.
- In between: left alone, so it doesn't hunt.

After a change it waits two readings (the relay's level covers the last second, partly the old gain),
except for clipping hard, which is never left to wait.
The total is split with the LNA (the RF stage, which sets the noise figure) taking 8 dB steps up to
32 dB first while the VGA keeps at least 6 dB, then the VGA (2 dB steps, to 62); the LNA's last step
(40) only once the VGA would run out. The RF amp is left as set: switching +14 dB in by itself could
overload the front end on a strong signal."""

TARGET = (-26.0, -14.0)          # dB full scale, average power
CLIP_FAST, CLIP_SLOW = 0.002, 0.0002
AUTO_MAX = 76                    # the most it turns up to (LNA 32 + VGA 44)
LNA_STEPS = tuple(range(0, 41, 8))
VGA_MAX = 62
TOTAL_MAX = 40 + VGA_MAX


def split(total: int) -> tuple[int, int]:
    """(LNA, VGA) for a total gain in dB."""
    total = max(0, min(TOTAL_MAX, int(total)))
    lna = min(32, max(0, (total - 6) // 8 * 8))
    if total - lna > VGA_MAX:
        lna = 40
    vga = max(0, min(VGA_MAX, (total - lna) // 2 * 2))
    return lna, vga


class AGC:
    def __init__(self, lna=16, vga=20):
        self.lna, self.vga = lna, vga
        self._wait = 0

    def step(self, level: dict | None) -> tuple[int, int] | None:
        """New (LNA, VGA) for this second's level, or None to keep them."""
        if not level:
            return None
        dbfs, clipped = float(level.get("dbfs", -100)), float(level.get("clipped", 0))
        if self._wait and clipped <= CLIP_FAST:
            self._wait -= 1
            return None
        total = self.lna + self.vga
        if clipped > CLIP_FAST:
            want = total - 8
        elif dbfs > TARGET[1] or clipped > CLIP_SLOW:
            want = total - 4
        elif dbfs < TARGET[0] and total < AUTO_MAX:
            want = min(AUTO_MAX, total + (10 if dbfs < TARGET[0] - 6 else 6))
        else:
            return None
        lna, vga = split(want)
        if (lna, vga) == (self.lna, self.vga):
            return None
        self.lna, self.vga = lna, vga
        self._wait = 2
        return lna, vga
