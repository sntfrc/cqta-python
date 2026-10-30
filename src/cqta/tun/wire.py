"""Wire format shared by the unprivileged app and the elevated helper (no Qt, no heavy imports).

Frames on the loopback socket: u16 big-endian length, u8 type, payload.
"""

from __future__ import annotations

import struct

T_STATUS = 0  # helper -> app: utf-8 "ok <ifname>" or "error <message>"
T_PACKET = 1  # either direction: one raw IPv4 packet
MAX_PAYLOAD = 65000


def encode(kind: int, payload: bytes) -> bytes:
    return struct.pack("!HB", len(payload), kind) + payload


class Reader:
    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        out = []
        while len(self._buf) >= 3:
            n, kind = struct.unpack("!HB", self._buf[:3])
            if len(self._buf) < 3 + n:
                break
            out.append((kind, bytes(self._buf[3:3 + n])))
            del self._buf[:3 + n]
        return out
