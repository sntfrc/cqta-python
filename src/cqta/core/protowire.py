"""Minimal protobuf wire-format reader/writer (only what the Meshtastic messages we use need)."""

from __future__ import annotations

import struct
from typing import Iterator, Union

Value = Union[int, bytes]  # varint / fixed32 / fixed64 -> int, length-delimited -> bytes


class ProtoWriter:
    def __init__(self) -> None:
        self.data = bytearray()

    def _varint(self, value: int) -> None:
        v = value & 0xFFFFFFFFFFFFFFFF
        while v >= 0x80:
            self.data.append((v & 0x7F) | 0x80)
            v >>= 7
        self.data.append(v)

    def _tag(self, field: int, wire_type: int) -> None:
        self._varint((field << 3) | wire_type)

    def uint(self, field: int, value: int) -> None:
        self._tag(field, 0)
        self._varint(value)

    def bool(self, field: int, value: bool) -> None:
        if value:
            self.uint(field, 1)

    def fixed32(self, field: int, value: int) -> None:
        self._tag(field, 5)
        self.data += struct.pack("<I", value & 0xFFFFFFFF)

    def fixed64(self, field: int, value: int) -> None:
        self._tag(field, 1)
        self.data += struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF)

    def bytes(self, field: int, value: bytes) -> None:
        self._tag(field, 2)
        self._varint(len(value))
        self.data += value

    def message(self, field: int, build) -> None:
        inner = ProtoWriter()
        build(inner)
        self.bytes(field, bytes(inner.data))

    def raw_field(self, field: int, value: Value, wire_type: int) -> None:
        """Re-emit a field read by ProtoReader (used to copy a message while changing one field)."""
        if wire_type == 2:
            self.bytes(field, value)  # type: ignore[arg-type]
        elif wire_type == 5:
            self.fixed32(field, value)  # type: ignore[arg-type]
        elif wire_type == 1:
            self.fixed64(field, value)  # type: ignore[arg-type]
        else:
            self.uint(field, value)  # type: ignore[arg-type]


class ProtoReader:
    """Iterates (field, wire_type, value); stops at end of input or on malformed data."""

    def __init__(self, data: bytes) -> None:
        self._d = bytes(data)
        self._i = 0

    def _varint(self):
        result = shift = 0
        while self._i < len(self._d) and shift < 64:
            b = self._d[self._i]
            self._i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
        return None

    def _take(self, n: int):
        if n < 0 or len(self._d) - self._i < n:
            return None
        out = self._d[self._i:self._i + n]
        self._i += n
        return out

    def __iter__(self) -> Iterator[tuple[int, int, Value]]:
        while self._i < len(self._d):
            key = self._varint()
            if key is None:
                return
            field, wt = key >> 3, key & 7
            if wt == 0:
                v = self._varint()
                if v is None:
                    return
                yield field, wt, v
            elif wt == 1:
                b = self._take(8)
                if b is None:
                    return
                yield field, wt, struct.unpack("<Q", b)[0]
            elif wt == 2:
                n = self._varint()
                if n is None or n > 0x7FFFFFFF:
                    return
                b = self._take(n)
                if b is None:
                    return
                yield field, wt, b
            elif wt == 5:
                b = self._take(4)
                if b is None:
                    return
                yield field, wt, struct.unpack("<I", b)[0]
            else:
                return
