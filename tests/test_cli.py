import os
import signal
import socket
import threading

import pytest

from cqta import cli
from cqta.app.settings import Settings
from cqta.tun import elevate


def parse(*argv):
    parser = cli.build_parser()
    args = parser.parse_args(list(argv))
    return cli.build_settings(args, parser)


def test_explicit_mode_ignores_saved_settings(tmp_path, monkeypatch):
    saved = Settings()
    saved.transport = "kiss"
    saved.kiss.host = "saved.example"
    saved.callsign = "IU4AAJ"
    saved.save(tmp_path / "s.json")
    monkeypatch.setattr(cli, "saved_settings_path", lambda: tmp_path / "s.json")
    s = parse("--tunnel", "--mode", "kiss", "--kiss-port", "9000")
    assert (s.kiss.host, s.kiss.tcp_port, s.callsign) == ("127.0.0.1", 9000, "N0CALL")
    assert s.system_interface and not s.tftp_server
    s = parse("--tunnel")
    assert s.kiss.host == "saved.example" and s.callsign == "IU4AAJ" and s.system_interface


def test_mode_options():
    s = parse("--tunnel", "--mode", "meshtastic", "--meshtastic-tcp", "10.0.0.5:5000")
    assert (s.mesh.kind, s.mesh.address, s.mesh.port) == ("tcp", "10.0.0.5", 5000)
    s = parse("--tunnel", "--mode", "meshtastic", "--meshtastic-serial", "COM3", "--callsign", "iu4aaj", "--no-beacon")
    assert (s.mesh.kind, s.mesh.address, s.callsign, s.beacon_enabled) == ("serial", "COM3", "IU4AAJ", False)
    s = parse("--tunnel", "--mode", "afsk", "--afsk-output", "USB", "--afsk-no-csma", "--afsk-tx-level", "0.5")
    assert (s.afsk.output_name, s.afsk.input_name, s.afsk.csma, s.afsk.tx_level) == ("USB", "", False, 0.5)


@pytest.mark.parametrize("argv", [
    ["--tunnel", "--kiss-port", "1"],
    ["--tunnel", "--mode", "kiss", "--afsk-burst", "2"],
    ["--tunnel", "--mode", "meshtastic"],
    ["--tunnel", "--mode", "kiss", "--kiss-port", "70000"],
    ["--tunnel", "--mode", "kiss", "--kiss-address", "nonsense"],
    ["--tunnel", "--mode", "network"],
])
def test_usage_errors(argv, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "saved_settings_path", lambda: tmp_path / "none.json")
    with pytest.raises(SystemExit) as e:
        parse(*argv)
    assert e.value.code == 2


def test_refuses_without_rights(monkeypatch, capsys):
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    assert cli.run_tunnel(parse("--tunnel", "--mode", "kiss")) == 1
    assert "administrator rights" in capsys.readouterr().err


def test_tunnel_runs_until_interrupted(monkeypatch, capsys):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    events = []

    class FakeBridge:
        def __init__(self):
            self.on_packet = self.on_closed = None

        def start(self, ip, mask, mtu):
            events.append(("start", ip, mask))
            return "tun9"

        def write(self, packet):
            pass

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(elevate, "is_elevated", lambda: True)
    monkeypatch.setattr("cqta.app.controller.TunBridge", FakeBridge)
    monkeypatch.setattr(elevate, "prompt_allowed", True)
    settings = parse("--tunnel", "--mode", "kiss", "--kiss-port", str(server.getsockname()[1]),
                     "--kiss-address", "10.9.9.9/24")
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    threading.Timer(2.0, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    try:
        code = cli.run_tunnel(settings)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        server.close()
    out = capsys.readouterr().out
    assert code == 0 and "Tunnel up: interface tun9" in out and "Stopped." in out
    assert events == [("start", "10.9.9.9", "255.255.255.0"), "stop"]
    assert elevate.prompt_allowed is False
    elevate.prompt_allowed = True
