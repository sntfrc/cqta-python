import time

from cqta.app.controller import Controller
from cqta.app.settings import Settings
from cqta.core import meshwire as mw
from cqta.core.protowire import ProtoReader, ProtoWriter
from cqta.transports.mesh import MeshConfig
from cqta.transports.mesh_links import MeshLink
from tests.test_transports import wait


class LimitedNode(MeshLink):
    region = 3
    last = None

    def __init__(self):
        super().__init__()
        LimitedNode.last = self
        self.override = False
        self.answer = True

    def open(self):
        pass

    def close(self):
        self._closed.set()

    def _emit(self, build):
        w = ProtoWriter()
        build(w)
        self.on_message(bytes(w.data))

    def _lora(self):
        w = ProtoWriter()
        w.uint(7, self.region)
        if self.override:
            w.bool(12, True)
        return bytes(w.data)

    def write(self, data):
        for f, _, v in ProtoReader(data):
            if f == 3:
                self._emit(lambda w: w.message(3, lambda m: m.uint(1, 0x1234)))
                self._emit(lambda w: w.message(5, lambda c: c.bytes(6, self._lora())))
                self._emit(lambda w: w.uint(7, v))
            elif f == 1 and self.answer:
                pkt = mw.parse_packet(v)
                if pkt.portnum == mw.PORTNUM_ADMIN_APP:
                    for af, _, av in ProtoReader(pkt.payload):
                        if af == 34:
                            self.override = mw.overrides_duty_cycle(mw.lora_config_in_config(av))

                    def ack(w):
                        def p(m):
                            m.fixed32(1, 0)

                            def d(x):
                                x.uint(1, mw.PORTNUM_ROUTING_APP)
                                x.fixed32(6, pkt.id)
                            m.message(4, d)
                        w.message(2, p)
                    self._emit(ack)


def make(monkeypatch, region=3):
    LimitedNode.region = region
    monkeypatch.setattr(MeshConfig, "make_link", lambda self: LimitedNode())
    s = Settings()
    s.transport = "meshtastic"
    s.mesh = MeshConfig("tcp", "x")
    ctrl = Controller(s)
    events = []
    ctrl.on_duty = lambda *a: events.append(a)
    ctrl.on_duty_failed = lambda m: events.append(("failed", m))
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    ctrl.start()
    ctrl.post(ctrl.connect)
    return ctrl, events


def test_duty_cycle_state_follows_node(monkeypatch):
    ctrl, events = make(monkeypatch)
    try:
        assert wait(lambda: (True, False, False) in events)
        ctrl.post(lambda: ctrl.set_duty_cycle_override(True))
        assert wait(lambda: (True, False, True) in events)
        assert wait(lambda: events[-1] == (True, True, False))
        ctrl.post(ctrl.disconnect)
        assert wait(lambda: events[-1] == (False, False, False))
    finally:
        ctrl.shutdown()


def test_duty_cycle_not_limited_region(monkeypatch):
    ctrl, events = make(monkeypatch, region=1)
    try:
        assert wait(lambda: (False, False, False) in events)
        assert not any(e[0] is True for e in events)
    finally:
        ctrl.shutdown()
