from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from ..core.afsk_port import AFSKPort, PortSettings
from ..core.dispatcher import Dispatcher, Timer
from ..core.ipv4 import (KISS_UDP_BLACKLIST, Address, bytes_to_ip, parse_address, random_ip,
                         should_drop)
from . import audio as au
from .base import LinkState, Reconnector, Transport, TransportInfo

MTU = 512
STATUS_INTERVAL = 1.0


@dataclass
class AFSKConfig:
    input_name: str = ""  # empty selects the system default
    input_api: str = ""
    output_name: str = ""
    output_api: str = ""
    address: str = field(default_factory=random_ip)
    tx_delay_ms: int = 500
    tx_tail_ms: int = 60
    tx_level: float = 0.8
    csma: bool = True
    max_burst_frames: int = 4
    clear_holdoff_ms: int = 600
    clear_jitter_ms: int = 600

    def port_settings(self) -> PortSettings:
        return PortSettings(self.tx_delay_ms / 1000, self.tx_tail_ms / 1000, self.tx_level, self.csma,
                            self.max_burst_frames, self.clear_holdoff_ms / 1000, self.clear_jitter_ms / 1000)


def valid_frame(frame: bytes) -> bool:
    return len(frame) >= 20 and frame[0] >> 4 == 4 and ((frame[2] << 8) | frame[3]) == len(frame)


class AFSKTransport(Transport):
    kind = "afsk"

    def __init__(self, dispatcher: Dispatcher, config: AFSKConfig,
                 audio_factory: Optional[Callable[[Optional[int], Optional[int]], au.AudioIO]] = None) -> None:
        super().__init__(dispatcher)
        self.config = config
        self._audio_factory = audio_factory or au.AudioIO
        self._audio: Optional[au.AudioIO] = None
        self._port: Optional[AFSKPort] = None
        self._address: Optional[Address] = None
        self._running = False
        self._status_timer: Optional[Timer] = None
        self._reconnect = Reconnector(dispatcher, self._open)
        self.input_level = 0.0
        self.channel_busy = False
        self.transmitting = False

    def start(self) -> None:
        if self._running:
            return
        address = parse_address(self.config.address)
        if address is None:
            self._set_state(LinkState.STOPPED, "Invalid IP address (use a.b.c.d/prefix)")
            raise ValueError("Invalid IP address; use a.b.c.d or a.b.c.d/prefix")
        self._address = address
        self._running = True
        self._reconnect.reset()
        self._open()

    def stop(self) -> None:
        self._running = False
        self._reconnect.cancel()
        self._close_audio()
        self._set_state(LinkState.STOPPED, "")

    def send(self, ip_packet: bytes) -> None:
        port = self._port
        if port is None or should_drop(ip_packet) or len(ip_packet) > MTU:
            return
        port.send(ip_packet)
        self.tx_packets += 1

    def set_tx_level(self, level: float) -> None:
        self.config.tx_level = level
        if self._port:
            self._port.settings.tx_level = level

    def apply_advanced(self) -> None:
        if self._port:
            self._port.settings = self.config.port_settings()

    @property
    def queued(self) -> int:
        return self._port.queued_frames if self._port else 0

    # Audio

    def _resolve(self, name: str, api: str, devices: list[au.AudioDevice]) -> Optional[int]:
        if not name:
            return None
        d = au.find_device(name, api, devices)
        if d is None:
            raise au.AudioError(f"Audio device '{name}' was not found")
        return d.index

    def _open(self) -> None:
        if not self._running:
            return
        self._close_audio()
        self._set_state(LinkState.CONNECTING, "Opening audio devices…")
        try:
            ins, outs = au.list_devices()
            audio = self._audio_factory(self._resolve(self.config.input_name, self.config.input_api, ins),
                                        self._resolve(self.config.output_name, self.config.output_api, outs))
            audio.on_samples = lambda s: self.dispatcher.post(lambda: self._on_samples(audio, s))
            audio.on_error = lambda m: self.dispatcher.post(lambda: self._audio_failed(audio, m))
            audio.start()
        except Exception as e:
            self._fail(str(e))
            return
        self._audio = audio
        port = AFSKPort(audio.input_rate, audio.output_rate, self.config.port_settings())
        port.on_frame = self._on_frame
        port.on_transmit = audio.play
        port.log = lambda m: self.log(m)
        self._port = port
        a = self._address
        self._set_info(TransportInfo(a.host, a.network, a.netmask, a.broadcast, MTU,
                                     f"AFSK 1200 baud, {audio.input_rate:.0f} Hz in / {audio.output_rate:.0f} Hz out"))
        self._status_timer = self.dispatcher.call_every(STATUS_INTERVAL, self._publish_status)
        self._set_state(LinkState.READY, f"Listening as {bytes_to_ip(a.host)}")

    def _close_audio(self) -> None:
        if self._status_timer:
            self._status_timer.cancel()
            self._status_timer = None
        audio, self._audio = self._audio, None
        self._port = None
        if audio is not None:
            audio.on_samples = lambda _: None

            def stop_audio() -> None:
                try:
                    audio.stop()
                except Exception:
                    pass

            # Closing a PortAudio stream can block for a long time (notably on Windows); never stall the core on it
            threading.Thread(target=stop_audio, name="cqta-audio-stop", daemon=True).start()

    def _fail(self, reason: str) -> None:
        self._close_audio()
        delay = self._reconnect.schedule()
        self._set_state(LinkState.CONNECTING, f"{reason}. Retrying in {delay:.0f} s…")
        self.log(f"AFSK: {reason}")

    def _audio_failed(self, audio, message: str) -> None:
        if audio is self._audio and self._running:
            self._fail(message)

    def _on_samples(self, audio, samples: np.ndarray) -> None:
        if audio is self._audio and self._port is not None:
            self._port.receive(samples)

    def _on_frame(self, frame: bytes) -> None:
        a = self._address
        if not valid_frame(frame) or should_drop(frame):
            return
        src, dst = frame[12:16], frame[16:20]
        if src == a.host or (dst != a.host and dst != a.broadcast):
            return
        self._deliver(frame)

    def _publish_status(self) -> None:
        if self._audio and self._port:
            self.input_level = self._audio.input_level
            self.channel_busy = self._port.is_busy
            self.transmitting = self._port.is_transmitting
            self.on_state(self.state, self._status_text())

    def _status_text(self) -> str:
        if self.transmitting:
            return "Transmitting"
        return "Receiving signal" if self.channel_busy else "Listening"
