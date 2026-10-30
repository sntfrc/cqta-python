from cqta.app.chat import ChatModel
from cqta.core.dispatcher import Dispatcher
from cqta.core.ipv4 import parse_udp


def pair():
    d = Dispatcher()
    a, b = ChatModel(d), ChatModel(d)
    drop = {"on": False}
    a.callsign, b.callsign = "AA1AA", "BB2BB"
    a.beacon_enabled = b.beacon_enabled = False
    a.source_ip = lambda: bytes([10, 0, 0, 1])
    b.source_ip = lambda: bytes([10, 0, 0, 2])
    for src, dst in ((a, b), (b, a)):
        def tx(pkt, dst=dst, src=src):
            if drop["on"]:
                return
            dg = parse_udp(pkt)
            dst.receive(dg.payload, dg.source)
        src.transmit = tx
    return a, b, drop


def test_conversation_and_unread():
    a, b, _ = pair()
    assert a.open_qso("bb2bb") == "BB2BB"
    assert a.send_text("BB2BB", "hello")
    info = []
    b.on_conversation = info.append
    a.send_text("BB2BB", "again")
    assert b._convs["AA1AA"].unread == 2
    assert [l.text for l in b.conversation_lines("AA1AA") if l.kind == "peer"] == ["hello", "again"]
    b.set_active_peer("AA1AA")
    assert b._convs["AA1AA"].unread == 0
    assert b.station_ip("AA1AA") == bytes([10, 0, 0, 1])


def test_missing_and_retransmission():
    a, b, drop = pair()
    a.open_qso("BB2BB")
    a.send_text("BB2BB", "one")
    drop["on"] = True
    a.send_text("BB2BB", "two")
    drop["on"] = False
    a.send_text("BB2BB", "three")
    conv = b._convs["AA1AA"]
    assert conv.session.missing == [1]
    b.request_missing("AA1AA")
    texts = [l.text for l in b.conversation_lines("AA1AA") if l.kind == "peer"]
    assert "two" in texts and conv.session.missing == []


def test_qrt_and_listen_only():
    a, b, _ = pair()
    a.open_qso("BB2BB")
    a.send_text("BB2BB", "hi")
    assert a.close_qso("BB2BB")
    assert b._convs["AA1AA"].closed
    a.callsign = "N0CALL"
    assert not a.send_broadcast("x")


def test_broadcast_and_cq():
    a, b, _ = pair()
    got = []
    b.on_line = lambda peer, line: got.append((peer, line))
    a.call_cq()
    assert got[0][0] is None and got[0][1].cq_from == "AA1AA"
    big = "x" * 300
    assert not a.send_broadcast(big)
