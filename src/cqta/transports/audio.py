"""Audio I/O for the AFSK modem with independently chosen input and output devices (sounddevice / PortAudio)."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

PREFERRED_RATES = (48000.0, 44100.0)


class AudioError(Exception):
    pass


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api: str
    inputs: int
    outputs: int
    default_rate: float

    @property
    def label(self) -> str:
        return f"{self.name} ({self.host_api})"


def _sd():
    try:
        import sounddevice as sd
    except OSError as e:  # PortAudio library missing
        raise AudioError(f"Audio support is unavailable: {e}") from e
    return sd


def list_devices() -> tuple[list[AudioDevice], list[AudioDevice]]:
    """Returns (input devices, output devices)."""
    sd = _sd()
    apis = sd.query_hostapis()
    devices = []
    for i, d in enumerate(sd.query_devices()):
        devices.append(AudioDevice(i, d["name"], apis[d["hostapi"]]["name"], d["max_input_channels"],
                                   d["max_output_channels"], float(d["default_samplerate"])))
    return [d for d in devices if d.inputs > 0], [d for d in devices if d.outputs > 0]


def default_device_names() -> tuple[Optional[str], Optional[str]]:
    sd = _sd()
    try:
        names = [sd.query_devices(sd.default.device[k])["name"] if sd.default.device[k] >= 0 else None
                 for k in (0, 1)]
    except Exception:
        return None, None
    return names[0], names[1]


def find_device(name: str, host_api: str, devices: list[AudioDevice]) -> Optional[AudioDevice]:
    for d in devices:
        if d.name == name and d.host_api == host_api:
            return d
    for d in devices:
        if d.name == name:
            return d
    return None


class AudioIO:
    """Capture from `input_device` and play on `output_device` (None selects the system default)."""

    def __init__(self, input_device: Optional[int], output_device: Optional[int]) -> None:
        self.input_device = input_device
        self.output_device = output_device
        self.on_samples: Callable[[np.ndarray], None] = lambda _: None  # audio thread
        self.on_error: Callable[[str], None] = lambda _: None  # audio thread
        self.input_rate = 0.0
        self.output_rate = 0.0
        self.input_level = 0.0
        self._in = None
        self._out = None
        self._out_channels = 1
        self._lock = threading.Lock()
        self._fifo: deque[np.ndarray] = deque()
        self._offset = 0
        self._failed = False

    def start(self) -> None:
        sd = _sd()
        try:
            self._in, self.input_rate = self._open(sd, sd.InputStream, self.input_device, self._in_callback, "input")
            self._out, self.output_rate = self._open(sd, sd.OutputStream, self.output_device, self._out_callback,
                                                     "output")
        except Exception:
            self.stop()
            raise

    def _open(self, sd, cls, device, callback, kind):
        info = sd.query_devices(device, kind)
        rates = [r for r in PREFERRED_RATES] + [float(info["default_samplerate"])]
        last: Exception | None = None
        for rate in dict.fromkeys(rates):
            for channels in (1, 2):
                if channels > info[f"max_{kind}_channels"] and channels != 1:
                    continue
                try:
                    stream = cls(device=device, samplerate=rate, channels=channels, dtype="float32",
                                 blocksize=1024 if kind == "input" else 0, callback=callback,
                                 finished_callback=None)
                    if kind == "output":
                        self._out_channels = channels
                    else:
                        self._in_channels = channels
                    stream.start()
                    return stream, rate
                except Exception as e:
                    last = e
        raise AudioError(f"Cannot open the audio {kind} device '{info['name']}': {last}")

    def stop(self) -> None:
        for s in (self._in, self._out):
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
        self._in = self._out = None
        with self._lock:
            self._fifo.clear()
            self._offset = 0

    def play(self, samples: np.ndarray) -> None:
        with self._lock:
            self._fifo.append(np.asarray(samples, dtype=np.float32))

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return bool(self._fifo)

    def _in_callback(self, indata, frames, time_info, status) -> None:
        mono = indata[:, 0].copy() if indata.shape[1] == 1 else indata.mean(axis=1).astype(np.float32)
        self.input_level = float(np.max(np.abs(mono))) if len(mono) else 0.0
        self.on_samples(mono)

    def _out_callback(self, outdata, frames, time_info, status) -> None:
        mono = np.zeros(frames, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < frames and self._fifo:
                head = self._fifo[0]
                n = min(frames - filled, len(head) - self._offset)
                mono[filled:filled + n] = head[self._offset:self._offset + n]
                filled += n
                self._offset += n
                if self._offset >= len(head):
                    self._fifo.popleft()
                    self._offset = 0
        if outdata.shape[1] == 1:
            outdata[:, 0] = mono
        else:
            outdata[:] = mono[:, None]
