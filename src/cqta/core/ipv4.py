"""IPv4 / UDP / ICMP helpers, packet filtering, and address handling."""

from __future__ import annotations

import ipaddress
import random
import struct
from dataclasses import dataclass
from typing import Optional

UDP_BLACKLIST = frozenset({1900, 5353, 9001, 64512})  # SSDP, mDNS, Yggdrasil, cjdns
TCP_BLACKLIST = frozenset({5900})  # VNC
PROTOCOL_BLACKLIST = frozenset({0x02, 0x80})  # IGMP, SSCOPMCE
# Extra discovery chatter dropped on KISS links (same set as pytap3's kisstun).
KISS_UDP_BLACKLIST = frozenset({67, 68, 137, 138, 3702, 5355})


def checksum(data: bytes) -> int:
    total = 0
    n = len(data)
    for i in range(0, n - 1, 2):
        total += (data[i] << 8) | data[i + 1]
    if n & 1:
        total += data[-1] << 8
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def should_drop(p: bytes, extra_udp: frozenset = frozenset(), drop_multicast: bool = False) -> bool:
    """True if the packet must NOT be forwarded over the radio. Non-IPv4 / malformed packets are dropped."""
    if len(p) < 20 or p[0] >> 4 != 4:
        return True
    hl = (p[0] & 0x0F) * 4
    if hl < 20 or len(p) < hl:
        return True
    proto = p[9]
    if proto in PROTOCOL_BLACKLIST:
        return True
    if drop_multicast and 224 <= p[16] <= 239:
        return True
    if proto in (6, 17) and len(p) >= hl + 4:
        dst = (p[hl + 2] << 8) | p[hl + 3]
        if proto == 17:
            return dst in UDP_BLACKLIST or dst in extra_udp
        return dst in TCP_BLACKLIST
    return False


def destination_of(p: bytes) -> Optional[bytes]:
    return bytes(p[16:20]) if len(p) >= 20 else None


def ip_to_bytes(address: str) -> bytes:
    return bytes(int(x) for x in address.split("."))


def bytes_to_ip(b: bytes) -> str:
    return ".".join(str(x) for x in b)


@dataclass
class Datagram:
    source: bytes
    destination: bytes
    source_port: int
    destination_port: int
    payload: bytes


def build_udp(source: bytes, destination: bytes, source_port: int, destination_port: int,
              payload: bytes) -> bytes:
    """UDP checksum is left at 0 ("not computed"), which IPv4 permits."""
    udp_length = 8 + len(payload)
    total = 20 + udp_length
    header = bytearray(struct.pack("!BBHHHBBH", 0x45, 0, total, 0, 0x4000, 64, 17, 0) + source + destination)
    struct.pack_into("!H", header, 10, checksum(bytes(header)))
    udp = struct.pack("!HHHH", source_port, destination_port, udp_length, 0)
    return bytes(header) + udp + payload


def parse_udp(data: bytes) -> Optional[Datagram]:
    if len(data) < 28 or data[0] >> 4 != 4 or data[9] != 17:
        return None
    hl = (data[0] & 0x0F) * 4
    if hl < 20 or len(data) < hl + 8:
        return None
    sport, dport, udp_length, _ = struct.unpack_from("!HHHH", data, hl)
    if udp_length < 8 or hl + udp_length > len(data):
        return None
    return Datagram(bytes(data[12:16]), bytes(data[16:20]), sport, dport, bytes(data[hl + 8:hl + udp_length]))


def build_icmp(icmp_type: int, source: bytes, destination: bytes, ident: int, seq: int,
               payload: bytes) -> bytes:
    body = bytearray(struct.pack("!BBHHH", icmp_type, 0, 0, ident, seq) + payload)
    struct.pack_into("!H", body, 2, checksum(bytes(body)))
    total = 20 + len(body)
    header = bytearray(struct.pack("!BBHHHBBH", 0x45, 0, total, 0, 0x4000, 64, 1, 0) + source + destination)
    struct.pack_into("!H", header, 10, checksum(bytes(header)))
    return bytes(header) + bytes(body)


@dataclass
class IcmpEcho:
    kind: int  # 8 request, 0 reply
    source: bytes
    destination: bytes
    ident: int
    seq: int


def parse_icmp_echo(data: bytes) -> Optional[IcmpEcho]:
    if len(data) < 28 or data[0] >> 4 != 4 or data[9] != 1:
        return None
    hl = (data[0] & 0x0F) * 4
    if hl < 20 or len(data) < hl + 8 or data[hl] not in (0, 8):
        return None
    ident, seq = struct.unpack_from("!HH", data, hl + 4)
    return IcmpEcho(data[hl], bytes(data[12:16]), bytes(data[16:20]), ident, seq)


def icmp_reply(request: bytes) -> Optional[bytes]:
    echo = parse_icmp_echo(request)
    if echo is None or echo.kind != 8:
        return None
    hl = (request[0] & 0x0F) * 4
    out = bytearray(request)
    out[hl] = 0
    out[hl + 2:hl + 4] = b"\0\0"
    struct.pack_into("!H", out, hl + 2, checksum(bytes(out[hl:])))
    out[12:16], out[16:20] = request[16:20], request[12:16]
    out[10:12] = b"\0\0"
    struct.pack_into("!H", out, 10, checksum(bytes(out[:hl])))
    return bytes(out)


@dataclass(frozen=True)
class Address:
    """An IPv4 host address with its subnet."""

    host: bytes
    prefix_length: int

    @property
    def _interface(self) -> ipaddress.IPv4Interface:
        return ipaddress.IPv4Interface(f"{bytes_to_ip(self.host)}/{self.prefix_length}")

    @property
    def netmask(self) -> bytes:
        return self._interface.netmask.packed

    @property
    def network(self) -> bytes:
        return self._interface.network.network_address.packed

    @property
    def broadcast(self) -> bytes:
        return self._interface.network.broadcast_address.packed

    @property
    def address_string(self) -> str:
        return bytes_to_ip(self.host)

    @property
    def network_string(self) -> str:
        return bytes_to_ip(self.network)

    @property
    def netmask_string(self) -> str:
        return bytes_to_ip(self.netmask)


def random_ip() -> str:
    return f"10.115.{random.randint(1, 254)}.{random.randint(1, 254)}/16"


def parse_address(raw: str) -> Optional[Address]:
    """Parse "a.b.c.d[/prefix]"; without a prefix the classful mask is inferred."""
    parts = raw.strip().split("/")
    if len(parts) > 2:
        return None
    octets = parts[0].split(".")
    if len(octets) != 4 or not all(o.isascii() and o.isdigit() and int(o) <= 255 for o in octets):
        return None
    host = bytes(int(o) for o in octets)
    if not 0 < host[0] < 224:
        return None
    if len(parts) == 2:
        if not (parts[1].isascii() and parts[1].isdigit()) or not 1 <= int(parts[1]) <= 30:
            return None
        prefix = int(parts[1])
    else:
        prefix = 8 if host[0] <= 126 else 16 if host[0] <= 191 else 24
    address = Address(host, prefix)
    if address.host in (address.network, address.broadcast):
        return None
    return address
