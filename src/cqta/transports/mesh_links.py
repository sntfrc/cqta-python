"""Byte-level links to a Meshtastic node: serial, TCP and BLE.

A link delivers whole FromRadio protobuf messages and accepts whole ToRadio messages. Callbacks fire on worker threads.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..core import meshwire as mw

START1 = 0x94
START2 = 0xC3
MAX_FRAME = 512
TCP_PORT = 4403


class LinkError(Exception):
    pass


class MeshLink:
    def __init__(self) -> None:
        self.on_message: Callable[[bytes], None] = lambda _: None
        self.on_closed: Callable[[str], None] = lambda _: None
        self._closed = threading.Event()

    def open(self) -> None:
        """Blocking; raises LinkError."""
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def _lost(self, reason: str) -> None:
        if not self._closed.is_set():
            self._closed.set()
            self.on_closed(reason)


class StreamFramer:
    """0x94 0xC3 <u16 big-endian length> <protobuf>, resynchronising on garbage (e.g. boot log text)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        buf = self._buf
        while True:
            i = buf.find(bytes([START1]))
            if i < 0:
                buf.clear()
                break
            del buf[:i]
            if len(buf) < 2:
                break
            if buf[1] != START2:
                del buf[:1]
                continue
            if len(buf) < 4:
                break
            n = (buf[2] << 8) | buf[3]
            if n > MAX_FRAME:
                del buf[:1]
                continue
            if len(buf) < 4 + n:
                break
            out.append(bytes(buf[4:4 + n]))
            del buf[:4 + n]
        return out


def frame(data: bytes) -> bytes:
    return bytes([START1, START2, len(data) >> 8, len(data) & 0xFF]) + data


class SerialLink(MeshLink):
    def __init__(self, port: str, baud: int = 115200) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self._serial = None
        self._write_lock = threading.Lock()

    def open(self) -> None:
        import serial

        try:
            # DTR and RTS stay asserted together, as the Meshtastic tools do: USB-CDC nodes (nRF52, ESP32-S3) only talk
            # while DTR is high, and with both lines high the auto-reset circuit of ESP32 boards does not trigger.
            s = serial.Serial()
            s.port = self.port
            s.baudrate = self.baud
            s.timeout = 0.2
            s.write_timeout = 5
            s.dtr = True
            s.rts = True
            s.open()
        except (serial.SerialException, OSError, ValueError) as e:
            raise LinkError(f"Cannot open {self.port}: {e}") from e
        self._serial = s
        try:
            s.write(bytes([START2]) * 32)  # wake a node that is in light sleep
            s.flush()
            time.sleep(0.2)  # give a node that just woke (or restarted because the port opened) time to listen
            s.reset_input_buffer()
        except (serial.SerialException, OSError) as e:
            s.close()
            raise LinkError(str(e)) from e
        threading.Thread(target=self._read_loop, name="cqta-serial", daemon=True).start()

    def _read_loop(self) -> None:
        framer = StreamFramer()
        s = self._serial
        try:
            while not self._closed.is_set():
                data = s.read(s.in_waiting or 1)
                if data:
                    for m in framer.feed(data):
                        self.on_message(m)
        except Exception as e:
            self._lost(f"Serial port lost: {e}")

    def write(self, data: bytes) -> None:
        try:
            with self._write_lock:
                self._serial.write(frame(data))
                self._serial.flush()
        except Exception as e:
            self._lost(f"Serial write failed: {e}")

    def close(self) -> None:
        self._closed.set()
        try:
            if self._serial is not None:
                self._serial.close()
        except Exception:
            pass


class TcpLink(MeshLink):
    def __init__(self, host: str, port: int = TCP_PORT) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._write_lock = threading.Lock()

    def open(self) -> None:
        try:
            s = socket.create_connection((self.host, self.port), timeout=10)
        except OSError as e:
            raise LinkError(f"Cannot connect to {self.host}:{self.port}: {e}") from e
        s.settimeout(None)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = s
        threading.Thread(target=self._read_loop, name="cqta-mesh-tcp", daemon=True).start()

    def _read_loop(self) -> None:
        framer = StreamFramer()
        try:
            while not self._closed.is_set():
                data = self._sock.recv(4096)
                if not data:
                    raise ConnectionError("connection closed by the node")
                for m in framer.feed(data):
                    self.on_message(m)
        except Exception as e:
            self._lost(f"TCP connection lost: {e}")

    def write(self, data: bytes) -> None:
        try:
            with self._write_lock:
                self._sock.sendall(frame(data))
        except Exception as e:
            self._lost(f"TCP write failed: {e}")

    def close(self) -> None:
        self._closed.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass


class BleLink(MeshLink):
    """Meshtastic GATT: write ToRadio, read FromRadio until empty whenever FromNum notifies."""

    def __init__(self, address: str) -> None:
        super().__init__()
        self.address = address
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._io_lock: Optional[asyncio.Lock] = None
        self._drain_wanted = False

    def open(self) -> None:
        started = threading.Event()

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._io_lock = asyncio.Lock()
            started.set()
            self._loop.run_forever()
            self._loop.close()

        self._thread = threading.Thread(target=run, name="cqta-ble", daemon=True)
        self._thread.start()
        started.wait()
        try:
            asyncio.run_coroutine_threadsafe(self._connect(), self._loop).result(timeout=45)
        except Exception as e:
            self.close()
            raise LinkError(f"Cannot connect over Bluetooth: {e or type(e).__name__}") from e

    async def _connect(self) -> None:
        from bleak import BleakClient

        client = BleakClient(self.address, disconnected_callback=lambda _c: self._lost("Bluetooth disconnected"),
                             timeout=30)
        await client.connect()
        self._client = client
        await client.start_notify(mw.FROM_NUM_UUID, lambda _c, _d: self._request_drain())
        self._request_drain()

    def _request_drain(self) -> None:
        if self._loop is not None and not self._closed.is_set():
            self._loop.call_soon_threadsafe(lambda: self._loop.create_task(self._drain()))

    async def _drain(self) -> None:
        try:
            async with self._io_lock:
                while not self._closed.is_set():
                    data = bytes(await self._client.read_gatt_char(mw.FROM_RADIO_UUID))
                    if not data:
                        break
                    self.on_message(data)
        except Exception as e:
            self._lost(f"Bluetooth read failed: {e}")

    async def _write(self, data: bytes) -> None:
        try:
            async with self._io_lock:
                await self._client.write_gatt_char(mw.TO_RADIO_UUID, data, response=True)
            await self._drain()
        except Exception as e:
            self._lost(f"Bluetooth write failed: {e}")

    def write(self, data: bytes) -> None:
        if self._loop is not None and not self._closed.is_set():
            asyncio.run_coroutine_threadsafe(self._write(data), self._loop)

    def close(self) -> None:
        self._closed.set()
        loop = self._loop
        if loop is None:
            return

        async def shutdown() -> None:
            try:
                if self._client is not None:
                    await asyncio.wait_for(self._client.disconnect(), 5)
            except Exception:
                pass
            loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop)
        except RuntimeError:
            pass


@dataclass(frozen=True)
class MeshDevice:
    kind: str  # "serial" | "ble"
    address: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.address})" if self.name and self.name != self.address else self.address


def list_serial_ports() -> list[MeshDevice]:
    from serial.tools import list_ports

    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    # macOS lists every port twice; opening the /dev/tty.* one blocks waiting for a carrier signal
    ports = [p for p in ports if not p.device.startswith("/dev/tty.")]
    return [MeshDevice("serial", p.device, p.description if p.description and p.description != "n/a" else p.device)
            for p in ports]


class BleScanner:
    """Continuous scan for devices advertising the Meshtastic service (like the iOS app's live list).

    `on_device(address, name, rssi)` fires on the scanner thread for every advertisement; `on_error(message)` if the
    scan cannot run (no adapter, Bluetooth off, permission denied).
    """

    def __init__(self, on_device: Callable[[str, str, int], None], on_error: Callable[[str], None]) -> None:
        self.on_device = on_device
        self.on_error = on_error
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), name="ble-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop, stop = self._loop, self._stop
        self._thread = None
        if loop is not None and stop is not None:
            loop.call_soon_threadsafe(stop.set)

    async def _run(self) -> None:
        from bleak import BleakScanner

        self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
        stop = self._stop

        def seen(device, adv) -> None:
            if stop.is_set():
                return
            self.on_device(device.address, adv.local_name or device.name or "Meshtastic", int(adv.rssi))

        try:
            async with BleakScanner(seen, service_uuids=[mw.SERVICE_UUID]):
                await stop.wait()
        except Exception as e:
            if not stop.is_set():
                self.on_error(f"Bluetooth scan failed: {e}")
