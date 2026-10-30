"""Received-file storage and the TFTP model (runs on the dispatcher thread)."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..core import protocol as proto
from ..core import tftp
from ..core.ipv4 import Datagram, build_udp, parse_udp

TRANSFER_TIMEOUT = 15.0


@dataclass(frozen=True)
class ReceivedFile:
    path: str
    name: str
    size: int
    modified: float


def default_download_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "CQta"


def sanitize_name(raw: str) -> str:
    name = raw.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ord(ch) >= 32 and ord(ch) != 127)
    name = re.sub(r'[<>:"|?*]', "_", name).strip(" .") if os.name == "nt" else name
    name = name.lstrip(".")
    return name or "received.bin"


def _size_text(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} bytes"


class FilesModel:
    def __init__(self, dispatcher) -> None:
        self.dispatcher = dispatcher
        self.engine = tftp.TFTPEngine()
        self.engine.timeout = TRANSFER_TIMEOUT
        self.engine.send = self._send_datagram
        self.engine.store_file = self._store
        self.engine.on_change = self._changed
        self.engine.on_incoming_started = self._incoming_started
        self.folder = default_download_dir()

        self.mtu = 200
        self.link_ready = False
        self.transmit: Callable[[bytes], None] = lambda _: None
        self.source_ip: Callable[[], bytes] = lambda: bytes(4)
        self.resolve_callsign: Callable[[str], Optional[bytes]] = lambda c: None
        self.display_name: Callable[[str], str] = lambda ip: ip

        self.on_transfers: Callable[[list[tftp.TransferInfo]], None] = lambda t: None
        self.on_files: Callable[[list[ReceivedFile]], None] = lambda f: None
        self.on_status: Callable[[str], None] = lambda m: None
        self.on_file_received: Callable[[str, int], None] = lambda name, size: None
        self.on_incoming_started: Callable[[str, str], None] = lambda name, peer: None
        self.notify_enabled: Callable[[str], bool] = lambda kind: False
        self.on_notification: Callable[[str, str, str, Optional[str]], None] = lambda k, t, b, p: None
        self._timer = None

    def start(self) -> None:
        self._timer = self.dispatcher.call_every(1.0, self._tick)
        self.refresh()

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()

    def _tick(self) -> None:
        self.engine.mtu = self.mtu
        self.engine.tick()

    @property
    def server_enabled(self) -> bool:
        return self.engine.server_enabled

    def set_server(self, enabled: bool) -> None:
        self.engine.server_enabled = enabled

    def set_folder(self, folder: str) -> None:
        self.folder = Path(folder)
        self.refresh()

    def resolve(self, target: str) -> Optional[bytes]:
        t = target.strip()
        parts = t.split(".")
        if len(parts) == 4 and all(p.isdigit() and int(p) < 256 for p in parts):
            return bytes(int(p) for p in parts)
        return self.resolve_callsign(t.upper())

    def send_file(self, path: str, target: str) -> Optional[str]:
        """Returns an error message, or None if the upload started."""
        if not self.link_ready:
            return "The tunnel is not connected."
        ip = self.resolve(target)
        if ip is None:
            call = target.strip().upper()
            if proto.is_valid_callsign(call):
                return (f"No known IP address for {call}. It is only known after that station has been heard "
                        "on CQta; enter the IP address instead.")
            return "Enter an IPv4 address or a callsign."
        try:
            data = Path(path).read_bytes()
            self.engine.mtu = self.mtu
            self.engine.upload(Path(path).name, data, ip)
        except (OSError, tftp.TFTPError) as e:
            return str(e)
        return None

    def cancel(self, transfer_id: int) -> None:
        self.engine.cancel(transfer_id)

    def handle(self, d: Datagram) -> bool:
        self.engine.mtu = self.mtu
        me = self.source_ip()
        # Only requests addressed to us; ignore broadcasts.
        if d.destination_port == tftp.PORT and d.destination != me:
            return False
        return self.engine.handle(d)

    def delete(self, path: str) -> None:
        p = Path(path)
        if p.parent == self.folder:
            try:
                p.unlink()
            except OSError as e:
                self.on_status(f"Could not delete {p.name}: {e}")
        self.refresh()

    def refresh(self) -> None:
        files = []
        try:
            for p in self.folder.iterdir():
                if p.is_file():
                    st = p.stat()
                    files.append(ReceivedFile(str(p), p.name, st.st_size, st.st_mtime))
        except OSError:
            pass
        files.sort(key=lambda f: -f.modified)
        self.on_files(files)

    def _changed(self) -> None:
        self.on_transfers(self.engine.transfers)
        self.refresh()

    def _incoming_started(self, name: str, peer_ip: str, total: Optional[int]) -> None:
        peer = self.display_name(peer_ip)
        self.on_incoming_started(name, peer)
        if self.notify_enabled("file_start"):
            self.on_notification("file_start", "Receiving a file", f"{sanitize_name(name)} from {peer}", None)

    def _send_datagram(self, local_port: int, destination: bytes, destination_port: int, payload: bytes) -> None:
        try:
            self.transmit(build_udp(self.source_ip(), destination, local_port, destination_port, payload))
        except Exception as e:
            self.on_status(f"File transfer transmit failed: {e}")

    def _store(self, raw_name: str, data: bytes) -> Optional[str]:
        name = sanitize_name(raw_name)
        stem, ext = os.path.splitext(name)
        candidate, n = name, 1
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            # "x" mode never overwrites, even if a file appears between the check and the write.
            while True:
                try:
                    with open(self.folder / candidate, "xb") as f:
                        f.write(data)
                    break
                except FileExistsError:
                    candidate = f"{stem} ({n}){ext}"
                    n += 1
        except OSError as e:
            self.on_status(f"Could not save {candidate}: {e}")
            return None
        self.on_file_received(candidate, len(data))
        if self.notify_enabled("file"):
            self.on_notification("file", "File received", f"{candidate} ({_size_text(len(data))})", candidate)
        return candidate
