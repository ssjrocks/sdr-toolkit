"""Audio to decoded messages: ToneDetector -> Keyer -> MorseDecoder."""

import time

import numpy as np

from .decoder import Message, MorseDecoder
from .dsp import RATE, Keyer, ToneDetector


class Engine:
    def __init__(self, freq: float = 700.0, bandwidth: float = 100.0, rate: int = RATE):
        self.detector = ToneDetector(freq, bandwidth, rate)
        self.keyer = Keyer()
        self.decoder = MorseDecoder()
        self.ratio = np.zeros(0)   # the last chunk's tone fraction, for the scope
        self.level = np.zeros(0)
        self.keyed = np.zeros(0, dtype=bool)

    def feed(self, audio: np.ndarray, now: float | None = None) -> list[Message]:
        """Process audio; returns messages that finished in it.

        The transmission in progress is available from ``current()``.
        """
        now = time.time() if now is None else now
        ratio, level = self.detector.process(audio)
        self.ratio, self.level = ratio, level
        keyed = np.empty(len(ratio), dtype=bool)
        done = []
        # Feed in 1 ms steps so the end-of-message check sees each silence.
        for i in range(len(ratio)):
            for tone, dur in self.keyer.feed(ratio[i : i + 1], level[i : i + 1]):
                self.decoder.add_run(tone, dur, now)
            keyed[i] = self.keyer.state
            if (
                not self.keyer.state
                and self.decoder.runs
                and self.keyer.current_run_ms >= self.decoder.end_gap_ms()
            ):
                msg = self.decoder.finish()
                if msg and msg.text:
                    done.append(msg)
        self.keyed = keyed
        return done

    def current(self) -> Message | None:
        silence = 0 if self.keyer.state else self.keyer.current_run_ms
        return self.decoder.current(silence)


def decode_audio(audio: np.ndarray, rate: int = RATE, **kw) -> list[Message]:
    """Decode a whole recording (for tests and files)."""
    eng = Engine(rate=rate, **kw)
    msgs = []
    for i in range(0, len(audio), rate // 20):
        msgs += eng.feed(audio[i : i + rate // 20], now=i / rate)
    msgs += eng.feed(np.zeros(rate * 4, dtype=np.float32), now=len(audio) / rate)
    return msgs
