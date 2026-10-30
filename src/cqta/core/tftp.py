"""TFTP (RFC 1350 with RFC 2347/2348/2349 options blksize and tsize): lock-step client (WRQ) and server (WRQ only).

Driven by received datagrams and `tick`. Not thread-safe; call from one thread. Time is passed in so it is testable.
"""

from __future__ import annotations

import enum
import random
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .ipv4 import Datagram, bytes_to_ip

PORT = 69
CLIENT_PORTS = (49152, 65000)
DEFAULT_BLOCK = 512


class TFTPError(Exception):
    pass


@dataclass
class Request:
    write: bool
    filename: str
    mode: str
    options: dict[str, str]


@dataclass
class Data:
    block: int
    payload: bytes


@dataclass
class Ack:
    block: int


@dataclass
class Error:
    code: int
    message: str


@dataclass
class OAck:
    options: dict[str, str]


Packet = Request | Data | Ack | Error | OAck


def _z(s: str) -> bytes:
    return s.encode("utf-8") + b"\0"


def _enc_options(options: dict[str, str]) -> bytes:
    return b"".join(_z(k) + _z(v) for k, v in sorted(options.items()))


def encode(p: Packet) -> bytes:
    if isinstance(p, Request):
        return struct.pack("!H", 2 if p.write else 1) + _z(p.filename) + _z(p.mode) + _enc_options(p.options)
    if isinstance(p, Data):
        return struct.pack("!HH", 3, p.block) + p.payload
    if isinstance(p, Ack):
        return struct.pack("!HH", 4, p.block)
    if isinstance(p, Error):
        return struct.pack("!HH", 5, p.code) + _z(p.message)
    return struct.pack("!H", 6) + _enc_options(p.options)


def _strings(b: bytes) -> Optional[list[str]]:
    if b and b[-1] != 0:
        return None
    return [s.decode("utf-8", "replace") for s in b.split(b"\0")[:-1]] if b else []


def _options(s: list[str]) -> Optional[dict[str, str]]:
    if len(s) % 2:
        return None
    return {s[i].lower(): s[i + 1] for i in range(0, len(s), 2)}


def decode(data: bytes) -> Optional[Packet]:
    if len(data) < 2:
        return None
    opcode = (data[0] << 8) | data[1]
    if opcode in (1, 2):
        s = _strings(data[2:])
        if s is None or len(s) < 2:
            return None
        opts = _options(s[2:])
        return None if opts is None else Request(opcode == 2, s[0], s[1].lower(), opts)
    if opcode == 3:
        return Data((data[2] << 8) | data[3], bytes(data[4:])) if len(data) >= 4 else None
    if opcode == 4:
        return Ack((data[2] << 8) | data[3]) if len(data) >= 4 else None
    if opcode == 5:
        if len(data) < 4:
            return None
        text = data[4:].split(b"\0")[0]
        return Error((data[2] << 8) | data[3], text.decode("utf-8", "replace"))
    if opcode == 6:
        s = _strings(data[2:])
        opts = None if s is None else _options(s)
        return None if opts is None else OAck(opts)
    return None


class TransferState(enum.Enum):
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"


@dataclass
class TransferInfo:
    id: int
    is_upload: bool
    peer: str
    filename: str
    bytes: int = 0
    total: Optional[int] = None
    state: TransferState = TransferState.ACTIVE
    error: str = ""


@dataclass
class _Session:
    info: TransferInfo
    local_port: int
    peer_ip: bytes
    peer_port: int
    tid_locked: bool
    blksize: int = DEFAULT_BLOCK
    requested_blksize: int = DEFAULT_BLOCK
    block: int = 0  # upload: last block sent (0 while pending); download: next block expected
    final_block: bool = False
    data: bytes | bytearray = b""
    last_packet: bytes = b""
    last_sent: float = 0.0
    retries: int = 0
    dally_until: Optional[float] = None

    @property
    def is_upload(self) -> bool:
        return self.info.is_upload

    @property
    def is_active(self) -> bool:
        return self.info.state is TransferState.ACTIVE


class TFTPEngine:
    DALLY = 30.0
    KEPT_FINISHED = 20

    def __init__(self) -> None:
        self.mtu = 200
        self.timeout = 10.0
        self.max_retries = 8
        self.max_receive_size = 4_000_000
        self.max_incoming = 4
        self.server_enabled = False
        # Transmit a UDP payload from local_port to (destination, destination_port).
        self.send: Callable[[int, bytes, int, bytes], None] = lambda *a: None
        # Persist a received file; returns the stored name, or None if it could not be saved.
        self.store_file: Callable[[str, bytes], Optional[str]] = lambda name, data: None
        self.on_change: Callable[[], None] = lambda: None
        self.on_incoming_started: Callable[[str, str, Optional[int]], None] = lambda *a: None
        self._sessions: list[_Session] = []
        self._dirty = False
        self._next_id = 1

    @property
    def transfers(self) -> list[TransferInfo]:
        return [TransferInfo(**{**s.info.__dict__}) for s in self._sessions]

    @property
    def max_block_size(self) -> int:
        return min(max(self.mtu - 28 - 4, 8), 1400)

    # Client

    def upload(self, name: str, data: bytes, destination: bytes, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        if len(destination) != 4:
            raise TFTPError("Invalid destination address.")
        if len(data) // self.max_block_size + 1 > 65535:
            raise TFTPError("File is too large for this link.")
        info = TransferInfo(self._take_id(), True, bytes_to_ip(destination), name, total=len(data))
        s = _Session(info, self._free_port(), destination, PORT, False)
        s.data = bytes(data)
        s.requested_blksize = self.max_block_size
        self._sessions.append(s)
        self._transmit(s, Request(True, name, "octet", {"blksize": str(self.max_block_size),
                                                         "tsize": str(len(data))}), now)
        self._prune()
        self._flush()
        return info.id

    def cancel(self, transfer_id: int) -> None:
        for s in self._sessions:
            if s.info.id == transfer_id and s.is_active:
                self._send_raw(s, Error(0, "Cancelled"))
                self._fail(s, "Cancelled")
                self._flush()
                return

    # Receive

    def handle(self, d: Datagram, now: Optional[float] = None) -> bool:
        """Returns True if the datagram belonged to TFTP."""
        now = time.time() if now is None else now
        try:
            return self._handle(d, now)
        finally:
            self._flush()

    def _handle(self, d: Datagram, now: float) -> bool:
        packet = decode(d.payload)
        if packet is None:
            return False
        for s in self._sessions:
            if (s.local_port == d.destination_port and s.peer_ip == d.source
                    and (s.is_active or (s.dally_until or 0) > now)):
                self._process(s, packet, d.source_port, now)
                return True
        if d.destination_port != PORT or not self.server_enabled or not isinstance(packet, Request):
            return False
        if not packet.write:
            self._reject(d, 2, "Read not supported")
            return True
        self._start_download(d, packet.filename, packet.options, now)
        return True

    def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        for s in self._sessions:
            if s.is_active and now - s.last_sent >= self.timeout:
                s.retries += 1
                if s.retries > self.max_retries:
                    self._fail(s, "Timed out")
                else:
                    s.last_sent = now
                    self.send(s.local_port, s.peer_ip, s.peer_port, s.last_packet)
        self._sessions = [s for s in self._sessions if s.is_active or s.dally_until is None or s.dally_until > now]
        self._flush()

    # Internals

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def _process(self, s: _Session, packet: Packet, port: int, now: float) -> None:
        if s.tid_locked and port != s.peer_port:
            return
        if isinstance(packet, Error) and s.is_active:
            self._fail(s, f"Peer: {packet.message or 'error'}")
            return
        if not s.is_active and s.is_upload:
            return
        if s.is_upload:
            self._process_upload(s, packet, port, now)
        else:
            self._process_download(s, packet, now)

    def _process_upload(self, s: _Session, packet: Packet, port: int, now: float) -> None:
        if isinstance(packet, OAck) and s.block == 0:
            s.peer_port = port
            s.tid_locked = True
            blksize = DEFAULT_BLOCK
            value = packet.options.get("blksize")
            if value is not None:
                if not value.isdigit() or not 8 <= int(value) <= s.requested_blksize:
                    self._send_raw(s, Error(8, "Bad option"))
                    self._fail(s, "Peer sent an invalid block size")
                    return
                blksize = int(value)
            self._begin(s, blksize, now)
        elif isinstance(packet, Ack) and packet.block == 0 and s.block == 0:
            s.peer_port = port
            s.tid_locked = True
            self._begin(s, DEFAULT_BLOCK, now)
        elif isinstance(packet, Ack) and s.block >= 1 and packet.block == s.block:
            s.info.bytes = min(packet.block * s.blksize, len(s.data))
            self._dirty = True
            if s.final_block:
                s.info.state = TransferState.DONE
                s.info.bytes = len(s.data)
            else:
                s.block += 1
                self._send_block(s, now)

    def _begin(self, s: _Session, blksize: int, now: float) -> None:
        if blksize + 32 > self.mtu:
            self._fail(s, "Peer does not support block size negotiation; packets would exceed the link MTU")
            return
        if len(s.data) // blksize + 1 > 65535:
            self._send_raw(s, Error(3, "File too large"))
            self._fail(s, "File is too large for the negotiated block size")
            return
        s.blksize = blksize
        s.block = 1
        self._send_block(s, now)

    def _send_block(self, s: _Session, now: float) -> None:
        offset = (s.block - 1) * s.blksize
        chunk = bytes(s.data[offset:offset + s.blksize])
        s.final_block = len(chunk) < s.blksize
        self._transmit(s, Data(s.block, chunk), now)

    def _start_download(self, d: Datagram, filename: str, options: dict[str, str], now: float) -> None:
        for e in self._sessions:
            if (not e.is_upload and e.is_active and e.peer_ip == d.source and e.peer_port == d.source_port
                    and e.block == 1):
                self.send(e.local_port, e.peer_ip, e.peer_port, e.last_packet)
                return
        if sum(1 for e in self._sessions if not e.is_upload and e.is_active) >= self.max_incoming:
            self._reject(d, 0, "Busy")
            return
        tsize = int(options["tsize"]) if options.get("tsize", "").isdigit() else None
        if tsize is not None and tsize > self.max_receive_size:
            self._reject(d, 3, "File too large")
            return
        reply: dict[str, str] = {}
        blksize = DEFAULT_BLOCK
        requested = options.get("blksize", "")
        if requested.isdigit() and int(requested) >= 8:
            blksize = min(int(requested), self.max_block_size)
            reply["blksize"] = str(blksize)
        if tsize is not None:
            reply["tsize"] = str(tsize)
        info = TransferInfo(self._take_id(), False, bytes_to_ip(d.source), filename, total=tsize)
        s = _Session(info, self._free_port(), d.source, d.source_port, True)
        s.blksize = blksize
        s.block = 1
        s.data = bytearray()
        self._sessions.append(s)
        self._transmit(s, OAck(reply) if reply else Ack(0), now)
        self.on_incoming_started(filename, info.peer, tsize)
        self._prune()

    def _process_download(self, s: _Session, packet: Packet, now: float) -> None:
        if not isinstance(packet, Data):
            return
        n = packet.block
        if n == s.block - 1:
            self.send(s.local_port, s.peer_ip, s.peer_port, s.last_packet)
            return
        if not s.is_active or n != s.block:
            return
        if len(packet.payload) > s.blksize:
            self._send_raw(s, Error(4, "Block too large"))
            self._fail(s, "Peer sent an oversized block")
            return
        if len(s.data) + len(packet.payload) > self.max_receive_size or (
                n == 65535 and len(packet.payload) == s.blksize):
            self._send_raw(s, Error(3, "File too large"))
            self._fail(s, "File too large")
            return
        s.data += packet.payload
        s.block += 1
        s.info.bytes = len(s.data)
        self._dirty = True
        if len(packet.payload) < s.blksize:
            stored = self.store_file(s.info.filename, bytes(s.data))
            if stored is None:
                self._send_raw(s, Error(3, "Cannot save file"))
                self._fail(s, "Could not save the file")
                return
            s.info.filename = stored
            s.info.total = len(s.data)
            s.info.state = TransferState.DONE
            s.dally_until = now + self.DALLY
            s.data = b""
        self._transmit(s, Ack(n), now)

    def _transmit(self, s: _Session, packet: Packet, now: float) -> None:
        s.last_packet = encode(packet)
        s.last_sent = now
        s.retries = 0
        self.send(s.local_port, s.peer_ip, s.peer_port, s.last_packet)
        self._dirty = True

    def _send_raw(self, s: _Session, packet: Packet) -> None:
        self.send(s.local_port, s.peer_ip, s.peer_port, encode(packet))

    def _reject(self, d: Datagram, code: int, message: str) -> None:
        self.send(self._free_port(), d.source, d.source_port, encode(Error(code, message)))

    def _fail(self, s: _Session, message: str) -> None:
        s.info.state = TransferState.FAILED
        s.info.error = message
        s.data = b""
        self._dirty = True

    def _free_port(self) -> int:
        while True:
            port = random.randint(*CLIENT_PORTS)
            if all(s.local_port != port for s in self._sessions):
                return port

    def _prune(self) -> None:
        finished = [s for s in self._sessions if not s.is_active]
        if len(finished) > self.KEPT_FINISHED:
            drop = {id(s) for s in finished[:len(finished) - self.KEPT_FINISHED]}
            self._sessions = [s for s in self._sessions if id(s) not in drop]

    def _flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.on_change()
