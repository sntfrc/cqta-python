import socket
import threading
import time

from cqta.core import meshwire as mw
from cqta.core.dispatcher import Dispatcher
from cqta.core.ipv4 import build_udp, ip_to_bytes
from cqta.core.protowire import ProtoReader, ProtoWriter
from cqta.transports.base import LinkState
from cqta.transports.kiss import KissConfig, KissDecoder, KissTransport, kiss_encode
from cqta.transports.mesh import MeshConfig, MeshTransport
from cqta.transports.mesh_links import MeshLink, StreamFramer, frame


def wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_stream_framer_resync_and_split():
    f = StreamFramer()
    msg = b"hello world"
    data = b"boot log\r\n" + frame(msg) + b"\x94" + frame(b"x")
    out = []
    for i in range(len(data)):
        out += f.feed(data[i:i + 1])
    assert out == [msg, b"x"]


def test_kiss_codec():
    payload = bytes([0xC0, 1, 0xDB, 2, 0xDC, 0xDD])
    enc = kiss_encode(payload, port=2)
    assert enc[0] == enc[-1] == 0xC0 and enc[1] == 0x20
    assert KissDecoder().feed(b"\xc0" + enc) == [(2, 0, payload)]


class FakeNodeLink(MeshLink):
    instances: list["FakeNodeLink"] = []

    def __init__(self):
        super().__init__()
        self.sent: list[bytes] = []
        FakeNodeLink.instances.append(self)

    def open(self):
        pass

    def close(self):
        self._closed.set()

    def write(self, data):
        self.sent.append(data)
        for f, _, v in ProtoReader(data):
            if f == 3:
                w = ProtoWriter()
                w.message(3, lambda m: m.uint(1, 0x1234))
                self.on_message(bytes(w.data))
                w = ProtoWriter()
                w.uint(7, v)
                self.on_message(bytes(w.data))

    def inject_packet(self, payload: bytes):
        w = ProtoWriter()

        def pkt(p):
            p.fixed32(1, 0x5678)
            p.fixed32(2, 0xFFFFFFFF)

            def d(m):
                m.uint(1, mw.PORTNUM_IP_TUNNEL_APP)
                m.bytes(2, payload)
            p.message(4, d)
        w.message(2, pkt)
        self.on_message(bytes(w.data))


def test_mesh_transport_with_fake_node():
    disp = Dispatcher()
    disp.start()
    try:
        t = MeshTransport(disp, MeshConfig("tcp", "x"), link_factory=lambda c: FakeNodeLink())
        got, states = [], []
        t.on_packet = got.append
        t.on_state = lambda s, d: states.append(s)
        disp.post(t.start)
        assert wait(lambda: LinkState.READY in states)
        assert t.info.my_ip == bytes([10, 115, 0x12, 0x34])
        pkt = build_udp(ip_to_bytes("10.115.86.120"), t.info.broadcast, 1, 2, b"hi")
        link = FakeNodeLink.instances[-1]
        link.inject_packet(pkt)
        assert wait(lambda: got == [pkt])
        disp.post(lambda: t.send(build_udp(t.info.my_ip, t.info.broadcast, 1, 2, b"yo")))
        assert wait(lambda: len(link.sent) >= 2)
        disp.post(t.stop)
        assert wait(lambda: states[-1] is LinkState.STOPPED)
    finally:
        disp.stop()


def test_mesh_reconnects_after_link_loss():
    disp = Dispatcher()
    disp.start()
    FakeNodeLink.instances.clear()
    try:
        t = MeshTransport(disp, MeshConfig("tcp", "x"), link_factory=lambda c: FakeNodeLink())
        disp.post(t.start)
        assert wait(lambda: FakeNodeLink.instances and t.state is LinkState.READY)
        FakeNodeLink.instances[0]._lost("boom")
        assert wait(lambda: len(FakeNodeLink.instances) == 2 and t.state is LinkState.READY, 6)
        disp.post(t.stop)
    finally:
        disp.stop()


def test_kiss_transport_roundtrip():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received = []

    def serve():
        c, _ = server.accept()
        dec = KissDecoder()
        c.sendall(kiss_encode(build_udp(ip_to_bytes("10.9.0.2"), ip_to_bytes("10.9.0.1"), 5, 6, b"to you")))
        c.sendall(kiss_encode(build_udp(ip_to_bytes("10.9.0.2"), ip_to_bytes("10.9.0.77"), 5, 6, b"not me")))
        while len(received) < 1:
            received.extend(dec.feed(c.recv(4096)))
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    disp = Dispatcher()
    disp.start()
    try:
        t = KissTransport(disp, KissConfig("127.0.0.1", port, 0, "10.9.0.1/24"))
        got = []
        t.on_packet = got.append
        disp.post(t.start)
        assert wait(lambda: len(got) == 1)
        assert b"to you" in got[0]
        out = build_udp(ip_to_bytes("10.9.0.1"), ip_to_bytes("10.9.0.255"), 7, 8, b"out")
        disp.post(lambda: t.send(out))
        assert wait(lambda: len(received) == 1)
        assert received[0] == (0, 0, out)
        disp.post(t.stop)
    finally:
        disp.stop()
        server.close()
