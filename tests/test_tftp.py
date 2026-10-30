from cqta.core import tftp
from cqta.core.ipv4 import Datagram, ip_to_bytes

A = ip_to_bytes("10.115.0.1")
B = ip_to_bytes("10.115.0.2")


def link(a: tftp.TFTPEngine, a_ip: bytes, b: tftp.TFTPEngine, b_ip: bytes, drop=lambda n: False):
    queue: list[tuple[str, Datagram]] = []
    count = [0]

    def make(src_name, src_ip, dst_ip):
        def send(local_port, dest, dest_port, payload):
            count[0] += 1
            if drop(count[0]):
                return
            queue.append(("b" if src_name == "a" else "a",
                          Datagram(src_ip, dest, local_port, dest_port, payload)))
        return send

    a.send = make("a", a_ip, b_ip)
    b.send = make("b", b_ip, a_ip)
    return queue


def pump(queue, a, b, now=0.0):
    while queue:
        who, d = queue.pop(0)
        (a if who == "a" else b).handle(d, now)


def test_codec_roundtrip():
    for p in [tftp.Request(True, "x.txt", "octet", {"blksize": "100"}), tftp.Data(7, b"abc"),
              tftp.Ack(3), tftp.Error(1, "no"), tftp.OAck({"tsize": "5"})]:
        assert tftp.decode(tftp.encode(p)) == p
    assert tftp.decode(b"\x00") is None


def test_upload_download():
    client, server = tftp.TFTPEngine(), tftp.TFTPEngine()
    server.server_enabled = True
    stored = {}
    server.store_file = lambda n, d: stored.setdefault(n, d) and n
    q = link(client, A, server, B)
    data = bytes(range(256)) * 10
    client.upload("f.bin", data, B, 0.0)
    pump(q, client, server)
    assert stored["f.bin"] == data
    assert client.transfers[0].state is tftp.TransferState.DONE
    assert server.transfers[0].state is tftp.TransferState.DONE


def test_empty_and_exact_multiple():
    for size in (0, tftp.TFTPEngine().max_block_size * 3):
        client, server = tftp.TFTPEngine(), tftp.TFTPEngine()
        server.server_enabled = True
        stored = {}
        server.store_file = lambda n, d: stored.setdefault(n, d) is not None and n
        q = link(client, A, server, B)
        client.upload("f", b"z" * size, B, 0.0)
        pump(q, client, server)
        assert stored["f"] == b"z" * size


def test_retransmit_on_loss():
    client, server = tftp.TFTPEngine(), tftp.TFTPEngine()
    server.server_enabled = True
    stored = {}
    server.store_file = lambda n, d: stored.setdefault(n, d) is not None and n
    q = link(client, A, server, B, drop=lambda n: n in (3, 6))
    client.upload("f", b"q" * 700, B, 0.0)
    now = 0.0
    for _ in range(40):
        pump(q, client, server, now)
        now += 11
        client.tick(now)
        server.tick(now)
    assert stored["f"] == b"q" * 700


def test_server_disabled_ignores():
    client, server = tftp.TFTPEngine(), tftp.TFTPEngine()
    q = link(client, A, server, B)
    client.upload("f", b"1", B, 0.0)
    pump(q, client, server)
    assert client.transfers[0].state is tftp.TransferState.ACTIVE
    for i in range(1, 12):
        client.tick(i * 100.0)
    assert client.transfers[0].state is tftp.TransferState.FAILED
