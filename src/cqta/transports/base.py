"""Common transport interface (mirrors the iOS PacketTransport).

A transport moves raw IPv4 packets over some radio. All public methods are called on the dispatcher thread, and all
callbacks are invoked on the dispatcher thread. Blocking I/O lives on worker threads that `post` back.
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from ..core.dispatcher import Dispatcher, Timer


class LinkState(enum.Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    READY = "ready"


@dataclass(frozen=True)
class TransportInfo:
    my_ip: bytes
    network: bytes
    netmask: bytes
    broadcast: bytes
    mtu: int
    detail: str = ""


class Transport:
    kind = "transport"

    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher
        self.on_packet: Callable[[bytes], None] = lambda _: None
        self.on_state: Callable[[LinkState, str], None] = lambda s, d: None
        self.on_info: Callable[[TransportInfo], None] = lambda _: None
        self.log: Callable[[str], None] = lambda _: None
        self.info: Optional[TransportInfo] = None
        self.state = LinkState.STOPPED
        self.tx_packets = 0
        self.rx_packets = 0

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def send(self, ip_packet: bytes) -> None:
        raise NotImplementedError

    def _set_state(self, state: LinkState, detail: str = "") -> None:
        self.state = state
        self.on_state(state, detail)

    def _set_info(self, info: TransportInfo) -> None:
        self.info = info
        self.on_info(info)

    def _deliver(self, packet: bytes) -> None:
        self.rx_packets += 1
        self.on_packet(packet)


class Reconnector:
    """Exponential backoff (1 s up to 30 s) retry helper."""

    def __init__(self, dispatcher: Dispatcher, action: Callable[[], None]) -> None:
        self._dispatcher = dispatcher
        self._action = action
        self._timer: Optional[Timer] = None
        self._delay = 1.0

    def schedule(self) -> float:
        self.cancel()
        delay = self._delay
        self._delay = min(self._delay * 2, 30.0)
        self._timer = self._dispatcher.call_later(delay, self._action)
        return delay

    def reset(self) -> None:
        self._delay = 1.0

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


class Generation:
    """Lets worker threads discard callbacks that belong to a link that has since been replaced."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def bump(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    @property
    def current(self) -> int:
        return self._value
