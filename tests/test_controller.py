import time

from cqta.app.controller import Controller
from cqta.app.settings import Settings
from cqta.core.dispatcher import Dispatcher
from cqta.transports.base import LinkState, Transport, TransportInfo


class LoopTransport(Transport):
    def __init__(self, d, ip):
        super().__init__(d)
        self.ip = bytes(ip)
        self.peer = None

    def start(self):
        self._set_info(TransportInfo(self.ip, bytes([10, 9, 0, 0]), bytes([255, 255, 0, 0]),
                                     bytes([10, 9, 255, 255]), 200, "loop"))
        self._set_state(LinkState.READY, "ok")

    def stop(self):
        self._set_state(LinkState.STOPPED, "")

    def send(self, p):
        self.tx_packets += 1
        peer = self.peer
        self.dispatcher.post(lambda: peer._deliver(p))


def wait(cond, t=5):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_two_controllers(tmp_path):
    a = Controller(Settings(callsign="AA1AA", download_dir=str(tmp_path / "a")))
    b = Controller(Settings(callsign="BB2BB", tftp_server=True, download_dir=str(tmp_path / "b")))
    # Share one dispatcher so the loop transports can hand packets across.
    b.dispatcher = a.dispatcher
    b.chat.dispatcher = b.files.dispatcher = a.dispatcher
    ta, tb = LoopTransport(a.dispatcher, [10, 9, 0, 1]), LoopTransport(a.dispatcher, [10, 9, 0, 2])
    ta.peer, tb.peer = tb, ta
    for c, t in ((a, ta), (b, tb)):
        t.on_packet, t.on_state, t.on_info = c._on_packet, c._on_state, c._on_info
        c.transport = t
    a.dispatcher.start()
    pings = []
    a.on_ping = pings.append
    a.query(lambda: (a._apply_settings(a.settings), b._apply_settings(b.settings), ta.start(), tb.start()))

    a.query(lambda: a.chat.call_cq())
    assert wait(lambda: "AA1AA" in [s.callsign for s in b.chat.snapshot()[2]])

    a.query(lambda: (a.chat.open_qso("BB2BB"), a.chat.send_text("BB2BB", "hi")))
    assert wait(lambda: any(l.text == "hi" for l in b.chat.conversation_lines("AA1AA")))

    a.query(lambda: a.ping("BB2BB"))
    assert wait(lambda: any(p.state == "reply" for p in pings))

    f = tmp_path / "x.txt"
    f.write_bytes(b"data" * 200)
    assert a.query(lambda: a.files.send_file(str(f), "BB2BB")) is None
    assert wait(lambda: (tmp_path / "b" / "x.txt").exists() and
                (tmp_path / "b" / "x.txt").read_bytes() == b"data" * 200)
    a.dispatcher.stop()
