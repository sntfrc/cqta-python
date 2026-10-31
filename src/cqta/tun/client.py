"""Unprivileged side of the TUN helper: listens on loopback, launches the elevated helper, relays packets."""

from __future__ import annotations

import atexit
import hmac
import os
import secrets
import socket
import tempfile
import threading
from typing import Callable, Optional

from . import elevate, wire

CONNECT_TIMEOUT = 120.0  # includes time for the user to type a password


class TunBridge:
    def __init__(self) -> None:
        self.on_packet: Callable[[bytes], None] = lambda _: None  # worker thread
        self.on_closed: Callable[[str], None] = lambda _: None  # worker thread
        self.interface = ""
        self._sock: Optional[socket.socket] = None
        self._listener: Optional[socket.socket] = None
        self._proc = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    def start(self, ip: str, netmask: str, mtu: int) -> str:
        """Blocking (may wait for a password prompt). Returns the interface name; raises on failure."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(CONNECT_TIMEOUT)
        self._listener = listener
        port = listener.getsockname()[1]
        token = secrets.token_hex(16).encode()
        fd, token_file = tempfile.mkstemp(prefix="cqta-")
        with os.fdopen(fd, "wb") as f:
            f.write(token)
        os.chmod(token_file, 0o644 if os.name == "nt" else 0o600)
        try:
            self._proc = elevate.launch_elevated(
                elevate.helper_command([str(port), token_file, ip, netmask, str(mtu)]))
            conn = self._accept(listener, token)
        except Exception:
            self.stop()
            raise
        finally:
            try:
                os.remove(token_file)
            except OSError:
                pass
        atexit.register(self.stop)  # closing the socket makes the helper remove the interface even if the app exits abruptly
        self._sock = conn
        reader = wire.Reader()
        conn.settimeout(30)
        status = None
        while status is None:
            data = conn.recv(4096)
            if not data:
                self.stop()
                raise elevate.ElevationError("The helper exited unexpectedly")
            for kind, payload in reader.feed(data):
                if kind == wire.T_STATUS:
                    status = payload.decode("utf-8", "replace")
        conn.settimeout(None)
        if not status.startswith("ok "):
            self.stop()
            raise elevate.ElevationError(status.removeprefix("error ").strip() or "Cannot create the interface")
        self.interface = status[3:]
        threading.Thread(target=self._read_loop, args=(conn, reader), name="cqta-tun", daemon=True).start()
        return self.interface

    def _accept(self, listener: socket.socket, token: bytes) -> socket.socket:
        while True:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                raise elevate.ElevationError("Timed out waiting for the helper to start") from None
            conn.settimeout(5)
            try:
                line = b""
                while not line.endswith(b"\n") and len(line) < 128:
                    chunk = conn.recv(1)  # byte-wise so we never swallow the frames that follow the token
                    if not chunk:
                        break
                    line += chunk
                if hmac.compare_digest(line.strip(), token):
                    return conn
            except OSError:
                pass
            conn.close()  # some other local process; keep waiting for the real helper

    def _read_loop(self, conn: socket.socket, reader: wire.Reader) -> None:
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                for kind, payload in reader.feed(data):
                    if kind == wire.T_PACKET:
                        self.on_packet(payload)
        except OSError:
            pass
        if not self._stopped.is_set():
            self.on_closed("The network interface helper stopped")

    def write(self, packet: bytes) -> None:
        sock = self._sock
        if sock is None or len(packet) > wire.MAX_PAYLOAD:
            return
        try:
            with self._lock:
                sock.sendall(wire.encode(wire.T_PACKET, packet))
        except OSError:
            pass

    def stop(self) -> None:
        self._stopped.set()
        for s in (self._sock, self._listener):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._sock = self._listener = None
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
