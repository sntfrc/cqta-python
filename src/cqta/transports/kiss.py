"""KISS over TCP (e.g. Direwolf, a hardware TNC bridge). Like pytap3's kisstun, the KISS data frames carry raw IPv4."""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from typing import Optional

from ..core.dispatcher import Dispatcher
from ..core.ipv4 import KISS_UDP_BLACKLIST, Address, bytes_to_ip, parse_address, random_ip, should_drop
from .afsk import valid_frame
from .base import Generation, LinkState, Reconnector, Transport, TransportInfo

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
DEFAULT_MTU = 256


def kiss_encode(payload: bytes, port: int = 0, command: int = 0) -> bytes:
    out = bytearray([FEND, ((port & 0xF) << 4) | (command & 0xF)])
    for b in payload:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    out.append(FEND)
    return bytes(out)


class KissDecoder:
    """Yields (port, command, payload) tuples."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._escape = False

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        out = []
        for b in data:
            if b == FEND:
                if self._in_frame and len(self._buf) >= 1:
                    head = self._buf[0]
                    out.append((head >> 4, head & 0xF, bytes(self._buf[1:])))
                self._buf.clear()
                self._in_frame = True
                self._escape = False
            elif not self._in_frame:
                continue
            elif self._escape:
                self._buf.append(FEND if b == TFEND else FESC if b == TFESC else b)
                self._escape = False
            elif b == FESC:
                self._escape = True
            else:
                self._buf.append(b)
        return out


@dataclass
class KissConfig:
    host: str = "127.0.0.1"
    tcp_port: int = 8001
    kiss_port: int = 0
    address: str = field(default_factory=random_ip)
    mtu: int = DEFAULT_MTU


class KissTransport(Transport):
    kind = "kiss"

    def __init__(self, dispatcher: Dispatcher, config: KissConfig) -> None:
        super().__init__(dispatcher)
        self.config = config
        self._address: Optional[Address] = None
        self._sock: Optional[socket.socket] = None
        self._write_lock = threading.Lock()
        self._gen = Generation()
        self._running = False
        self._reconnect = Reconnector(dispatcher, self._connect)

    def start(self) -> None:
        if self._running:
            return
        address = parse_address(self.config.address)
        if address is None:
            raise ValueError("Invalid IP address; use a.b.c.d or a.b.c.d/prefix")
        self._address = address
        self._running = True
        self._reconnect.reset()
        self._connect()

    def stop(self) -> None:
        self._running = False
        self._reconnect.cancel()
        self._close()
        self._set_state(LinkState.STOPPED, "")

    def send(self, ip_packet: bytes) -> None:
        sock = self._sock
        if sock is None or should_drop(ip_packet, KISS_UDP_BLACKLIST, drop_multicast=True) \
                or len(ip_packet) > self.config.mtu:
            return
        data = kiss_encode(ip_packet, self.config.kiss_port)
        try:
            with self._write_lock:
                sock.sendall(data)
            self.tx_packets += 1
        except OSError as e:
            self._fail(self._gen.current, f"Connection lost: {e}")

    def _close(self) -> None:
        self._gen.bump()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _connect(self) -> None:
        if not self._running:
            return
        self._close()
        gen = self._gen.bump()
        c = self.config
        self._set_state(LinkState.CONNECTING, f"Connecting to {c.host}:{c.tcp_port}…")

        def work() -> None:
            try:
                s = socket.create_connection((c.host, c.tcp_port), timeout=10)
                s.settimeout(None)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as e:
                self.dispatcher.post(lambda: self._fail(gen, f"Cannot connect to {c.host}:{c.tcp_port}: {e}"))
                return
            self.dispatcher.post(lambda: self._connected(gen, s))
            decoder = KissDecoder()
            try:
                while True:
                    data = s.recv(4096)
                    if not data:
                        raise ConnectionError("connection closed by the TNC")
                    for frame in decoder.feed(data):
                        self.dispatcher.post(lambda f=frame: self._on_kiss(gen, f))
            except Exception as e:
                self.dispatcher.post(lambda: self._fail(gen, f"Connection lost: {e}"))

        threading.Thread(target=work, name="cqta-kiss", daemon=True).start()

    def _connected(self, gen: int, sock: socket.socket) -> None:
        if gen != self._gen.current or not self._running:
            sock.close()
            return
        self._sock = sock
        self._reconnect.reset()
        a = self._address
        self._set_info(TransportInfo(a.host, a.network, a.netmask, a.broadcast, self.config.mtu,
                                     f"KISS TCP {self.config.host}:{self.config.tcp_port}"))
        self._set_state(LinkState.READY, f"Connected, {bytes_to_ip(a.host)}")

    def _on_kiss(self, gen: int, frame: tuple[int, int, bytes]) -> None:
        port, command, payload = frame
        if gen != self._gen.current or command != 0 or port != self.config.kiss_port:
            return
        a = self._address
        if not valid_frame(payload) or should_drop(payload, KISS_UDP_BLACKLIST, drop_multicast=True):
            return
        if payload[12:16] == a.host or payload[16:20] not in (a.host, a.broadcast):
            return
        self._deliver(payload)

    def _fail(self, gen: int, reason: str) -> None:
        if gen != self._gen.current or not self._running:
            return
        self._close()
        delay = self._reconnect.schedule()
        self._set_state(LinkState.CONNECTING, f"{reason}. Retrying in {delay:.0f} s…")
        self.log(f"KISS: {reason}")
