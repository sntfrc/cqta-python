"""Plain UDP over an already configured operating system network interface (Ethernet, Wi-Fi, a VPN...).

The rest of the app speaks raw IPv4 packets, so outgoing UDP packets are unpacked and sent through ordinary sockets
(CQta chat goes to the interface's broadcast address, directed traffic to the peer), and datagrams read from those
sockets are wrapped back into IPv4/UDP packets. ICMP is not carried, so ping is unavailable on this link.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import threading
from dataclasses import dataclass
from typing import Optional

from ..core import ipv4
from ..core import protocol as proto
from ..core.dispatcher import Dispatcher
from ..core.ipv4 import Address, bytes_to_ip
from .base import LinkState, Transport, TransportInfo

MAX_MTU = 1500
TFTP_PORT = 69


@dataclass
class NetworkConfig:
    interface: str = ""


@dataclass(frozen=True)
class NetworkInterface:
    name: str
    address: str  # a.b.c.d
    prefix: int
    mtu: int

    @property
    def label(self) -> str:
        return f"{self.name} ({self.address}/{self.prefix})"


def list_interfaces() -> list[NetworkInterface]:
    """Up, non-loopback interfaces with an IPv4 address (one entry per address)."""
    import psutil

    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
    except Exception:
        return []
    found = []
    for name, entries in sorted(addrs.items()):
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        for a in entries:
            if a.family != socket.AF_INET or not a.address or not a.netmask:
                continue
            ip = ipaddress.IPv4Address(a.address)
            if ip.is_loopback or ip.is_unspecified:
                continue
            try:
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{a.netmask}").prefixlen
            except ValueError:
                continue
            if 1 <= prefix <= 30:
                found.append(NetworkInterface(name, a.address, prefix, min(st.mtu, MAX_MTU) if st and st.mtu else 1400))
    return found


def find_interface(name: str) -> Optional[NetworkInterface]:
    matches = [i for i in list_interfaces() if i.name == name]
    return matches[0] if matches else None


class NetworkTransport(Transport):
    kind = "network"
    supports_icmp = False

    def __init__(self, dispatcher: Dispatcher, config: NetworkConfig) -> None:
        super().__init__(dispatcher)
        self.config = config
        self._address: Optional[Address] = None
        self._socks: dict[int, list[socket.socket]] = {}  # per port; the first one is used for sending
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        if not self.config.interface:
            raise ValueError("Choose a network interface")
        iface = find_interface(self.config.interface)
        if iface is None:
            raise ValueError(f"Network interface '{self.config.interface}' is not available or has no IPv4 address")
        a = ipv4.parse_address(f"{iface.address}/{iface.prefix}")
        if a is None:
            raise ValueError(f"Unsupported address {iface.address}/{iface.prefix}")
        self._address = a
        self._running = True
        self._set_state(LinkState.CONNECTING, f"Opening {iface.name}…")
        try:
            self._open_port(proto.PORT, required=True)
        except OSError as e:
            self._running = False
            raise ValueError(f"Cannot listen on UDP port {proto.PORT}: {e}") from e
        self._open_port(TFTP_PORT)  # best effort; low ports can need privileges on some systems
        self._set_info(TransportInfo(a.host, a.network, a.netmask, a.broadcast, iface.mtu, f"Network {iface.label}"))
        self._set_state(LinkState.READY, f"Connected, {bytes_to_ip(a.host)} on {iface.name}")

    def stop(self) -> None:
        self._running = False
        with self._lock:
            socks, self._socks = [s for group in self._socks.values() for s in group], {}
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        self._set_state(LinkState.STOPPED, "")

    def send(self, ip_packet: bytes) -> None:
        d = ipv4.parse_udp(ip_packet)
        if d is None or not self._running or self._address is None:
            return
        sock = self._open_port(d.source_port)
        if sock is None:
            return
        dest = d.destination
        if dest == bytes([255] * 4) or dest[0] >= 224:
            dest = self._address.broadcast
        try:
            sock.sendto(d.payload, (bytes_to_ip(dest), d.destination_port))
            self.tx_packets += 1
        except OSError as e:
            self.log(f"Network: send failed: {e}")

    def _bind_addresses(self) -> list[str]:
        """Where to listen. Some systems (macOS, BSDs) do not hand broadcasts to a socket bound to 0.0.0.0 when
        other sockets share the port, so there we bind the interface's broadcast address and its own address
        (for directed traffic); Windows cannot bind a broadcast address, so it listens on all addresses."""
        a = self._address
        if sys.platform == "win32" or a is None:
            return [""]
        return [bytes_to_ip(a.host), bytes_to_ip(a.broadcast)]

    def _open_port(self, port: int, required: bool = False) -> Optional[socket.socket]:
        with self._lock:
            group = self._socks.get(port)
            if group:
                return group[0]
            opened: list[socket.socket] = []
            try:
                for address in self._bind_addresses():
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    opened.append(s)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32":
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)  # several instances on one machine
                    try:
                        s.bind((address, port))
                    except OSError:
                        if len(opened) == 1:
                            raise
                        s.bind(("", port))  # broadcast address not bindable here (e.g. loopback)
            except OSError as e:
                for s in opened:
                    s.close()
                if required:
                    raise
                self.log(f"Network: cannot use UDP port {port}: {e}")
                return None
            self._socks[port] = opened
        for s in opened:
            threading.Thread(target=self._reader, args=(port, s), name=f"cqta-net-{port}", daemon=True).start()
        return opened[0]

    def _reader(self, port: int, sock: socket.socket) -> None:
        while True:
            try:
                data, (host, sport) = sock.recvfrom(65535)
            except OSError:
                return
            self.dispatcher.post(lambda d=data, h=host, p=sport: self._received(port, h, p, d))

    def _received(self, port: int, host: str, sport: int, data: bytes) -> None:
        a = self._address
        if not self._running or a is None:
            return
        try:
            src = bytes(int(x) for x in host.split("."))
        except ValueError:
            return
        if len(src) != 4:
            return
        self._deliver(ipv4.build_udp(src, a.host, sport, port, data))
