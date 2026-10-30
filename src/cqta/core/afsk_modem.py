"""1200 baud Bell 202 AFSK modem: HDLC framing, NRZI, continuous-phase modulator, multi-slicer demodulator.

The air format matches the CQta iOS app: HDLC flags, bit-stuffed raw IPv4 packet, CRC-16/X-25, flag.
No AX.25 header. The demodulator is vectorized with numpy (the per-sample slicer PLLs are advanced
run-by-run between decision transitions), so it keeps up with real-time audio in pure Python.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np

BAUD = 1200.0
MARK_HZ = 1200.0
SPACE_HZ = 2200.0
FLAG = 0x7E


def crc16(data: bytes) -> int:
    """CRC-16/X-25 (reflected 0x1021, init 0xFFFF, final xor 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return ~crc & 0xFFFF


def _flag_bits() -> list[int]:
    return [(FLAG >> i) & 1 for i in range(8)]


def hdlc_bits(frames: list[bytes], preamble_flags: int, tail_flags: int) -> np.ndarray:
    """Bit stream (LSB-first, before NRZI) for a burst: preamble flags, frames separated by one flag, tail flags."""
    out: list[int] = []
    flag = _flag_bits()
    for _ in range(max(preamble_flags, 1)):
        out += flag
    for frame in frames:
        fcs = crc16(frame)
        body = bytes(frame) + bytes((fcs & 0xFF, fcs >> 8))
        ones = 0
        for byte in body:
            for i in range(8):
                bit = (byte >> i) & 1
                out.append(bit)
                if bit:
                    ones += 1
                    if ones == 5:
                        out.append(0)
                        ones = 0
                else:
                    ones = 0
        out += flag
    for _ in range(tail_flags):
        out += flag
    return np.array(out, dtype=np.uint8)


def hdlc_decode(bits, min_payload: int, max_payload: int) -> Optional[bytes]:
    """Remove bit stuffing from the bits between two flags and check the FCS. Returns the payload."""
    out = bytearray()
    acc = count = ones = 0
    for b in bits:
        if ones == 5:
            ones = 0
            if b == 0:
                continue  # stuffed zero
            return None  # six ones in a row inside a frame: invalid
        ones = ones + 1 if b else 0
        acc |= int(b) << count
        count += 1
        if count == 8:
            out.append(acc)
            acc = count = 0
    if count != 0 or not min_payload + 2 <= len(out) <= max_payload + 2:
        return None
    payload = bytes(out[:-2])
    fcs = out[-2] | (out[-1] << 8)
    return payload if crc16(payload) == fcs else None


class AFSKModulator:
    """Continuous-phase binary FSK with NRZI line coding (bit 0 = change tone, bit 1 = keep tone)."""

    def __init__(self, sample_rate: float) -> None:
        self.sample_rate = sample_rate
        self._phase = 0.0
        self._space = False
        self._carry = 0.0

    def modulate(self, bits: np.ndarray, amplitude: float) -> np.ndarray:
        if len(bits) == 0:
            return np.zeros(0, dtype=np.float32)
        spb = self.sample_rate / BAUD
        zeros = (bits == 0).astype(np.int64)
        space_state = (np.cumsum(zeros) + (1 if self._space else 0)) % 2 == 1
        self._space = bool(space_state[-1])
        edges = np.floor(self._carry + spb * np.arange(1, len(bits) + 1)).astype(np.int64)
        counts = np.diff(np.concatenate(([0], edges)))
        self._carry = (self._carry + spb * len(bits)) - edges[-1]
        freq = np.where(space_state, SPACE_HZ, MARK_HZ)
        inst = np.repeat(freq, counts)
        phase = self._phase + np.cumsum(2 * math.pi * inst / self.sample_rate)
        self._phase = float(phase[-1] % (2 * math.pi))
        out = (np.sin(phase) * amplitude).astype(np.float32)
        # Short raised-cosine ramps avoid key clicks at the start and end of the burst.
        ramp = min(len(out) // 2, int(self.sample_rate * 0.004))
        if ramp > 0:
            g = (0.5 - 0.5 * np.cos(np.pi * np.arange(ramp) / ramp)).astype(np.float32)
            out[:ramp] *= g
            out[len(out) - ramp:] *= g[::-1]
        return out


def _bandpass_taps(fs: float, low: float, high: float) -> np.ndarray:
    n = int(fs / 48000 * 191) | 1
    n = max(n, 31)
    m = np.arange(n) - (n - 1) / 2
    fh, fl = min(high, fs * 0.45) / fs, low / fs
    taps = 2 * fh * np.sinc(2 * fh * m) - 2 * fl * np.sinc(2 * fl * m)
    return taps * np.hamming(n)


class AFSKDemodulator:
    """Non-coherent AFSK demodulator built for imperfect audio paths (radio de/pre-emphasis, clock error, noise).

    Chain: band-pass -> quadrature correlators for mark and space over one symbol -> a bank of slicers with
    different mark/space energy weightings (handles spectral tilt) -> one digital PLL per slicer -> NRZI -> HDLC.
    Every slicer decodes independently; duplicates are merged, so a frame survives if at least one slicer
    gets it right.
    """

    DEFAULT_SLICER_GAINS = (0.25, 0.5, 1.0, 2.0, 4.0)
    ABSOLUTE_MIN = 2.5e-5

    def __init__(self, sample_rate: float, slicer_gains=DEFAULT_SLICER_GAINS,
                 min_payload: int = 20, max_payload: int = 1600) -> None:
        self.on_frame: Callable[[bytes], None] = lambda _: None
        self.fs = float(sample_rate)
        self.window = max(8, round(sample_rate / BAUD))
        self.min_payload = min_payload
        self.max_payload = max_payload
        self.max_bits = (max_payload + 4) * 8 * 6 // 5 + 64
        self.busy = False
        self.frames_decoded = 0
        self.sample_count = 0

        self._taps = _bandpass_taps(self.fs, 600.0, 3200.0)
        self._step_symbol = BAUD / self.fs
        self._piece = max(16, int(self.fs / 1000))
        self._level_alpha_per_sample = 1 / (0.02 * self.fs)
        self._floor_rise_per_sample = 1 / (30 * self.fs)
        self._scale = (2 / self.window) ** 2
        self._gains = list(slicer_gains)
        self._level = 0.0
        self._floor = 0.0
        self._recent: list[tuple[int, int]] = []
        self._reset_filters()
        self._slicers = [self._new_slicer(g) for g in self._gains]

    @property
    def is_warm(self) -> bool:
        return self.sample_count >= int(self.fs / 2)

    @staticmethod
    def _new_slicer(gain: float) -> dict:
        return {"gain": gain, "phase": 0.0, "last": False, "prev": False, "shift": 0, "bits": [], "locked": False}

    def _reset_filters(self) -> None:
        self._fir_tail = np.zeros(len(self._taps) - 1)
        self._mix_phase_m = 0.0
        self._mix_phase_s = 0.0
        self._hist = np.zeros((4, self.window))

    def reset(self) -> None:
        """Forget all signal state (call after our own transmission, which the receiver must not decode)."""
        self._reset_filters()
        for s in self._slicers:
            s["bits"] = []
            s["shift"] = 0
            s["locked"] = False
        self._level = self._floor
        self.busy = False
        self._recent.clear()

    def process(self, samples: np.ndarray) -> None:
        x = np.nan_to_num(np.asarray(samples, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
        n = len(x)
        if n == 0:
            return
        ext = np.concatenate((self._fir_tail, x))
        filtered = np.convolve(ext, self._taps, mode="valid")
        self._fir_tail = ext[len(ext) - (len(self._taps) - 1):]

        idx = np.arange(n)
        pm = self._mix_phase_m + idx * (2 * math.pi * MARK_HZ / self.fs)
        ps = self._mix_phase_s + idx * (2 * math.pi * SPACE_HZ / self.fs)
        self._mix_phase_m = float((self._mix_phase_m + n * 2 * math.pi * MARK_HZ / self.fs) % (2 * math.pi))
        self._mix_phase_s = float((self._mix_phase_s + n * 2 * math.pi * SPACE_HZ / self.fs) % (2 * math.pi))
        prod = np.stack((filtered * np.cos(pm), filtered * np.sin(pm),
                         filtered * np.cos(ps), filtered * np.sin(ps)))
        full = np.concatenate((self._hist, prod), axis=1)
        self._hist = full[:, n:]
        cs = np.cumsum(full, axis=1)
        sums = cs[:, self.window:] - cs[:, :-self.window]
        em = sums[0] ** 2 + sums[1] ** 2
        es = sums[2] ** 2 + sums[3] ** 2
        total = (em + es) * self._scale

        for start in range(0, n, self._piece):
            stop = min(start + self._piece, n)
            self.sample_count += stop - start
            self._update_level(float(total[start:stop].mean()), stop - start)
            for s in self._slicers:
                self._run_slicer(s, em[start:stop], es[start:stop])
            self._recent = [(h, at) for h, at in self._recent if self.sample_count - at <= int(self.fs * 0.02)]

    def _update_level(self, block_mean: float, length: int) -> None:
        alpha = 1 - math.exp(-length * self._level_alpha_per_sample)
        self._level += (block_mean - self._level) * alpha
        if self.sample_count < int(self.fs / 2):
            self._floor = self._level
            return
        if self._level < self._floor:
            self._floor = self._level
        else:
            self._floor += (self._level - self._floor) * (1 - math.exp(-length * self._floor_rise_per_sample))
        on_threshold = max(self._floor * 5, self.ABSOLUTE_MIN)
        off_threshold = max(self._floor * 3, self.ABSOLUTE_MIN * 0.5)
        if self.busy:
            if self._level < off_threshold:
                self.busy = False
                for s in self._slicers:
                    s["locked"] = False
        elif self._level > on_threshold:
            self.busy = True

    def _run_slicer(self, s: dict, em: np.ndarray, es: np.ndarray) -> None:
        state = (s["gain"] * es) > em
        n = len(state)
        change = np.flatnonzero(state[1:] != state[:-1]) + 1
        if state[0] != s["last"]:
            change = np.concatenate(([0], change))
        step = self._step_symbol
        pos = 0
        current = s["last"]
        for t in change:
            t = int(t)
            if t > pos:
                self._advance(s, current, t - pos, step)
                pos = t
            # The sliding-window discriminator crosses over half a symbol before the best sampling instant.
            inertia = 0.88 if s["locked"] else 0.6
            s["phase"] = s["phase"] * inertia + 0.5 * (1 - inertia)
            current = bool(state[t])
            s["last"] = current
        if n > pos:
            self._advance(s, current, n - pos, step)
        s["last"] = current

    def _advance(self, s: dict, state: bool, samples: int, step: float) -> None:
        total = s["phase"] + samples * step
        ticks = int(total)
        s["phase"] = total - ticks
        for _ in range(ticks):
            self._push(s, state)

    def _push(self, s: dict, state: bool) -> None:
        bit = 1 if state == s["prev"] else 0
        s["prev"] = state
        s["shift"] = (s["shift"] >> 1) | (bit << 7)
        bits = s["bits"]
        bits.append(bit)
        if s["shift"] == FLAG:
            s["locked"] = True
            body = bits[:max(len(bits) - 8, 0)]
            if len(body) >= (self.min_payload + 2) * 8:
                frame = hdlc_decode(body, self.min_payload, self.max_payload)
                if frame is not None:
                    self._emit(frame)
            s["bits"] = []
            s["shift"] = 0
        elif len(bits) > self.max_bits:
            s["bits"] = []

    def _emit(self, frame: bytes) -> None:
        # Several slicers usually decode the same frame within a few milliseconds of each other.
        h = hash(frame)
        if any(r[0] == h for r in self._recent):
            return
        self._recent.append((h, self.sample_count))
        self.frames_decoded += 1
        self.on_frame(frame)
