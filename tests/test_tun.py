import queue
import threading
import time

import pytest

import pytap3
from cqta.tun import client, elevate, helper


class FakeTap:
    def __init__(self, mtu=1500):
        self.name = "fake0"
        self.q = queue.Queue()
        self.written = []

    def up(self): pass
    def ifconfig(self, **kw): self.configured = kw
    def write(self, p): self.written.append(p)
    def read(self, size=None):
        p = self.q.get()
        if p is None:
            raise OSError("closed")
        return p
    def close(self): self.q.put(None)


def test_bridge_roundtrip(monkeypatch):
    tap = FakeTap()
    monkeypatch.setattr(pytap3, "TapDevice", lambda mtu=1500: tap)

    def fake_launch(cmd):
        args = cmd[cmd.index("--tun-helper") + 1:] if "--tun-helper" in cmd else cmd
        threading.Thread(target=helper.run, args=(args,), daemon=True).start()

    monkeypatch.setattr(elevate, "launch_elevated", fake_launch)
    monkeypatch.setattr(elevate, "helper_command", lambda a: a)
    bridge = client.TunBridge()
    got = []
    bridge.on_packet = got.append
    assert bridge.start("10.115.1.1", "255.255.0.0", 200) == "fake0"
    assert tap.configured["ip"] == "10.115.1.1"
    tap.q.put(b"\x45from-os")
    bridge.write(b"\x45to-os")
    end = time.time() + 3
    while time.time() < end and not (got and tap.written):
        time.sleep(0.01)
    assert got == [b"\x45from-os"] and tap.written == [b"\x45to-os"]
    bridge.stop()


def test_bridge_rejects_wrong_token(monkeypatch):
    import socket
    monkeypatch.setattr(client, "CONNECT_TIMEOUT", 1.0)

    def evil(cmd):
        port = int(cmd[0])
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"wrong\n")

    monkeypatch.setattr(elevate, "launch_elevated", evil)
    monkeypatch.setattr(elevate, "helper_command", lambda a: a)
    with pytest.raises(elevate.ElevationError):
        client.TunBridge().start("10.1.1.1", "255.0.0.0", 200)
