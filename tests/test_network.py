import socket
import time

from cqta.app.controller import Controller
from cqta.app.settings import Settings
from cqta.core import ipv4
from cqta.core import protocol as proto
from cqta.transports import network
from cqta.transports.base import LinkState
from cqta.transports.network import NetworkConfig, NetworkInterface, NetworkTransport
from tests.test_transports import wait


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_settings_roundtrip(tmp_path):
    s = Settings()
    s.transport = "network"
    s.network.interface = "en0"
    s.save(tmp_path / "s.json")
    loaded = Settings.load(tmp_path / "s.json")
    assert loaded.transport == "network" and loaded.network.interface == "en0"


def test_start_requires_interface(monkeypatch):
    monkeypatch.setattr(network, "list_interfaces", lambda: [])
    t = NetworkTransport(Controller(Settings()).dispatcher, NetworkConfig(""))
    for name in ("", "nope"):
        t.config.interface = name
        try:
            t.start()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_udp_roundtrip(monkeypatch):
    port = free_udp_port()
    monkeypatch.setattr(network, "list_interfaces", lambda: [NetworkInterface("lo0", "127.0.0.1", 24, 1400)])
    monkeypatch.setattr(network, "find_interface", lambda name: network.list_interfaces()[0])
    monkeypatch.setattr(proto, "PORT", port)
    monkeypatch.setattr(network, "TFTP_PORT", free_udp_port())
    ctrl = Controller(Settings())
    t = NetworkTransport(ctrl.dispatcher, NetworkConfig("lo0"))
    got = []
    t.on_packet = got.append
    ctrl.dispatcher.start()
    try:
        ctrl.query(t.start)
        assert t.state is LinkState.READY and t.info.broadcast == bytes([127, 0, 0, 255]) and not t.supports_icmp

        outside = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        outside.sendto(b"hello", ("127.0.0.1", port))
        wait(lambda: got)
        d = ipv4.parse_udp(got[0])
        assert d.payload == b"hello" and d.destination_port == port and d.destination == bytes([127, 0, 0, 1])

        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(3)
        pkt = ipv4.build_udp(bytes([127, 0, 0, 1]), bytes([127, 0, 0, 1]), port, listener.getsockname()[1], b"out")
        ctrl.query(lambda: t.send(pkt))
        data, _ = listener.recvfrom(100)
        assert data == b"out"
        ctrl.query(lambda: t.send(ipv4.build_icmp(8, bytes(4), bytes(4), 1, 1, b"x")))  # ignored, no exception
    finally:
        ctrl.query(t.stop)
        ctrl.dispatcher.stop() if hasattr(ctrl.dispatcher, "stop") else None
