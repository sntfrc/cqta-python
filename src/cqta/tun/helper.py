"""The elevated half: creates the OS TUN interface and bridges packets to the app over a loopback socket.

Invoked as `cqta --tun-helper PORT TOKEN_FILE IP NETMASK MTU`. The token lives in a 0600 file rather than on the
command line so other local users cannot see it in the process list and hijack the connection.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import threading

from . import wire


def _valid(port: str, ip: str, netmask: str, mtu: str) -> tuple[int, str, str, int]:
    p, m = int(port), int(mtu)
    if not (1024 <= p <= 65535 and 68 <= m <= 9000):
        raise ValueError("bad port or mtu")
    ipaddress.IPv4Address(ip)
    ipaddress.IPv4Address(netmask)
    return p, ip, netmask, m


def run(argv: list[str]) -> int:
    try:
        port, token_file, ip, netmask, mtu = argv
        port, ip, netmask, mtu = _valid(port, ip, netmask, mtu)
        with open(token_file, "rb") as f:
            token = f.read().strip()
        try:
            os.remove(token_file)
        except OSError:
            pass
    except Exception as e:
        print(f"cqta tun helper: bad arguments: {e}", file=sys.stderr)
        return 2

    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.settimeout(None)
    sock.sendall(token + b"\n")
    send_lock = threading.Lock()

    def send(kind: int, payload: bytes) -> None:
        with send_lock:
            sock.sendall(wire.encode(kind, payload))

    try:
        from pytap3 import TapDevice

        dev = TapDevice(mtu=mtu)
        dev.up()
        dev.ifconfig(ip=ip, netmask=netmask, mtu=mtu)
    except Exception as e:
        send(wire.T_STATUS, f"error {e}".encode())
        return 1
    send(wire.T_STATUS, f"ok {dev.name}".encode())

    stop = threading.Event()

    def tun_to_app() -> None:
        try:
            while not stop.is_set():
                pkt = dev.read()
                if pkt:
                    send(wire.T_PACKET, pkt)
        except Exception:
            pass
        finally:
            stop.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    threading.Thread(target=tun_to_app, daemon=True).start()
    reader = wire.Reader()
    try:
        while not stop.is_set():
            data = sock.recv(65536)
            if not data:
                break
            for kind, payload in reader.feed(data):
                if kind == wire.T_PACKET:
                    try:
                        dev.write(payload)
                    except Exception:
                        pass
    except OSError:
        pass
    stop.set()
    try:
        dev.close()
    except Exception:
        pass
    return 0  # the caller must exit with os._exit: the reader thread may be stuck in a read close() can't interrupt
