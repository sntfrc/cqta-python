"""Half-duplex AFSK modem port: frames in/out, audio samples in/out.

Time is derived from the number of input samples processed, so the whole thing is deterministic and testable.
Not thread-safe; call everything from one thread.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .afsk_modem import BAUD, AFSKDemodulator, AFSKModulator, hdlc_bits

MAX_QUEUE = 32
MAX_BUSY_WAIT = 15.0
MAX_BURST_BITS = 12000


@dataclass
class PortSettings:
    tx_delay: float = 0.5  # seconds of flags before the first frame (keys VOX, lets the receiver lock)
    tx_tail: float = 0.06  # seconds of flags after the last frame
    tx_level: float = 0.8
    csma: bool = True  # wait for a quiet channel (plus a random hold-off) before transmitting
    max_burst_frames: int = 4  # frames bundled into one key-up before pausing
    clear_holdoff: float = 0.6  # seconds of silence required before transmitting
    clear_jitter: float = 0.6  # extra random seconds (up to this) so stations don't collide
    burst_pause: float = 1.5  # seconds to stay silent after a transmission


class AFSKPort:
    def __init__(self, input_rate: float, output_rate: float, settings: PortSettings | None = None) -> None:
        self.settings = settings or PortSettings()
        self.on_frame: Callable[[bytes], None] = lambda _: None
        self.on_transmit: Callable[[np.ndarray], None] = lambda _: None
        self.log: Callable[[str], None] = lambda _: None
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.frames_sent = 0
        self.frames_received = 0
        self.frames_dropped = 0

        self._demod = AFSKDemodulator(input_rate)
        self._mod = AFSKModulator(output_rate)
        self._demod.on_frame = self._on_demod_frame
        self._queue: list[bytes] = []
        self._rx_time = 0.0
        self._tx_until = 0.0
        self._was_deaf = False
        self._quiet_since: float | None = None
        self._busy_since: float | None = None
        self._holdoff = 0.2
        self._rest_until = 0.0

    @property
    def is_busy(self) -> bool:
        return self._demod.busy

    @property
    def is_transmitting(self) -> bool:
        return self._rx_time < self._tx_until

    @property
    def queued_frames(self) -> int:
        return len(self._queue)

    def _on_demod_frame(self, frame: bytes) -> None:
        self.frames_received += 1
        self.on_frame(frame)

    def send(self, frame: bytes) -> None:
        """Queue a frame; transmission starts from `receive` once the channel is free."""
        if len(self._queue) >= MAX_QUEUE:
            self.frames_dropped += 1
            self.log("AFSK transmit queue full; dropping packet")
            return
        self._queue.append(bytes(frame))

    def receive(self, samples: np.ndarray) -> None:
        """Feed microphone / line-in samples (mono, at `input_rate`)."""
        if len(samples) == 0:
            return
        self._rx_time += len(samples) / self.input_rate
        if self._rx_time < self._tx_until:
            self._was_deaf = True
            return
        if self._was_deaf:
            # Ignore our own transmission and its echo.
            self._demod.reset()
            self._was_deaf = False
            self._quiet_since = None
            self._busy_since = None
        self._demod.process(samples)

        if self._demod.busy:
            self._quiet_since = None
            if self._busy_since is None:
                self._busy_since = self._rx_time
        else:
            self._busy_since = None
            if self._quiet_since is None and self._demod.is_warm:
                self._quiet_since = self._rx_time
                self._holdoff = self.settings.clear_holdoff + random.uniform(0, max(self.settings.clear_jitter, 0))
        self._transmit_if_allowed()

    def _transmit_if_allowed(self) -> None:
        st = self.settings
        if not self._queue or not self._demod.is_warm or self._rx_time < self._rest_until:
            return
        if st.csma:
            quiet_long_enough = self._quiet_since is not None and self._rx_time - self._quiet_since >= self._holdoff
            stuck_busy = self._busy_since is not None and self._rx_time - self._busy_since > MAX_BUSY_WAIT
            if not (quiet_long_enough or stuck_busy):
                return
        # Bundle queued frames into one key-up, but keep a burst to roughly ten seconds of airtime.
        count = bits = 0
        limit = min(len(self._queue), max(st.max_burst_frames, 1))
        while count < limit:
            frame_bits = (len(self._queue[count]) + 3) * 8 * 6 // 5
            if count > 0 and bits + frame_bits > MAX_BURST_BITS:
                break
            bits += frame_bits
            count += 1
        frames, self._queue = self._queue[:count], self._queue[count:]

        flags_per_second = BAUD / 8
        stream = hdlc_bits(frames, int(-(-st.tx_delay * flags_per_second // 1)),
                           int(-(-st.tx_tail * flags_per_second // 1)))
        samples = self._mod.modulate(stream, st.tx_level)
        # Include playback latency and room echo in the period during which we are deaf.
        self._tx_until = self._rx_time + len(samples) / self.output_rate + 0.25
        self._rest_until = self._tx_until + st.burst_pause
        self.frames_sent += len(frames)
        self._quiet_since = None
        self.on_transmit(samples)
