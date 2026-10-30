from cqta.app.files import FilesModel
from cqta.core.dispatcher import Dispatcher
from tests.test_chat import pair


def collect(model):
    got = []
    model.on_notification = lambda kind, title, body, peer: got.append((kind, title, body, peer))
    return got


def test_chat_notification_toggles():
    a, b, _ = pair()
    got = collect(b)
    b.notify = {"direct": False, "broadcast": False, "file_start": True, "file": True, "cq": False, "mention": False}
    a.open_qso("BB2BB")
    a.send_text("BB2BB", "hi")
    assert got == []
    b.notify["direct"] = True
    a.send_text("BB2BB", "again")
    assert [g[0] for g in got] == ["direct"] and got[0][3] == "AA1AA"

    got.clear()
    a.send_broadcast("hello world")
    assert got == []  # broadcasts off
    b.notify["broadcast"] = True
    a.send_broadcast("hello again")
    assert [g[0] for g in got] == ["broadcast"]

    got.clear()
    b.notify.update(broadcast=False, mention=True)
    a.send_broadcast("hi BB2BB how are you")
    a.send_broadcast("nothing for you")
    assert len(got) == 1 and "mentioned" in got[0][1]

    got.clear()
    b.notify.update(mention=False, cq=True)
    a.send_broadcast("CQ CQ de AA1AA")
    assert len(got) == 1 and got[0][1].startswith("CQ from")


def test_file_notifications(tmp_path):
    f = FilesModel(Dispatcher())
    f.folder = tmp_path
    got = collect(f)
    enabled = {"file_start": True, "file": True}
    f.notify_enabled = lambda k: enabled.get(k, False)
    f.display_name = lambda ip: "AA1AA"
    f._incoming_started("a.txt", "10.0.0.1", 10)
    assert got[-1][:3] == ("file_start", "Receiving a file", "a.txt from AA1AA")
    assert f._store("a.txt", b"x" * 2048) == "a.txt"
    assert got[-1] == ("file", "File received", "a.txt (2.0 KB)", "a.txt")
    enabled["file"] = enabled["file_start"] = False
    got.clear()
    f._incoming_started("b.txt", "10.0.0.1", 1)
    f._store("b.txt", b"y")
    assert got == []
