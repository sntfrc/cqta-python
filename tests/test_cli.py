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
    s = parse("--tunnel", "10.1.2.3/16", "--mode", "kiss", "--kiss-port", "9000", "--callsign", "n0call")
    assert (s.kiss.host, s.kiss.tcp_port, s.kiss.address, s.callsign) == ("127.0.0.1", 9000, "10.1.2.3/16", "N0CALL")
    assert not s.beacon_enabled
    assert s.system_interface and not s.tftp_server
    s = parse("--tunnel")
    assert s.kiss.host == "saved.example" and s.callsign == "IU4AAJ" and s.system_interface


def test_mode_options():
    s = parse("--tunnel", "--mode", "meshtastic", "--meshtastic-tcp", "10.0.0.5:5000", "--callsign", "N0CALL")
    assert (s.mesh.kind, s.mesh.address, s.mesh.port) == ("tcp", "10.0.0.5", 5000)
    s = parse("--tunnel", "--mode", "meshtastic", "--meshtastic-serial", "COM3", "--callsign", "iu4aaj")
    assert (s.mesh.kind, s.mesh.address, s.callsign, s.beacon_enabled) == ("serial", "COM3", "IU4AAJ", False)
    s = parse("--tunnel", "10.0.0.9", "--mode", "afsk", "--output-device", "USB", "--no-csma", "--tx-level", "0.5",
              "--callsign", "N0CALL", "--beacon-interval", "300")
    assert (s.afsk.output_name, s.afsk.input_name, s.afsk.csma, s.afsk.tx_level) == ("USB", "", False, 0.5)
    assert s.beacon_enabled and s.beacon_interval == 300


@pytest.mark.parametrize("argv", [
    ["--tunnel", "--kiss-port", "1"],
    ["--tunnel", "10.1.1.1/16", "--mode", "kiss", "--callsign", "N0CALL", "--burst", "2"],
    ["--tunnel", "--mode", "meshtastic", "--callsign", "N0CALL"],
    ["--tunnel", "10.1.1.1/16", "--mode", "kiss", "--callsign", "N0CALL", "--kiss-port", "70000"],
    ["--tunnel", "nonsense", "--mode", "kiss", "--callsign", "N0CALL"],
    ["--tunnel", "--mode", "kiss", "--callsign", "N0CALL"],
    ["--tunnel", "10.1.1.1/16", "--mode", "kiss"],
    ["--tunnel", "10.1.1.1/16", "--mode", "meshtastic", "--meshtastic-tcp", "h", "--callsign", "N0CALL"],
    ["--tunnel", "--mode", "network"],
])
def test_usage_errors(argv, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "saved_settings_path", lambda: tmp_path / "none.json")
    with pytest.raises(SystemExit) as e:
        parse(*argv)
    assert e.value.code == 2


def test_refuses_without_rights(monkeypatch, capsys):
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    assert cli.run_tunnel(parse("--tunnel", "10.1.1.1/16", "--mode", "kiss", "--callsign", "N0CALL")) == 1
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
    settings = parse("--tunnel", "10.9.9.9/24", "--mode", "kiss", "--kiss-port", str(server.getsockname()[1]),
                     "--callsign", "N0CALL")
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


def test_dump_config_roundtrip(monkeypatch):
    s = Settings()
    s.transport = "afsk"
    s.callsign = "IU4AAJ"
    s.afsk.input_name = "USB Audio CODEC"
    s.afsk.csma = False
    s.afsk.tx_level = 0.5
    s.beacon_enabled = False
    args = cli.dump_config_command(s)
    again = parse(*args)
    assert (again.afsk.input_name, again.afsk.address, again.afsk.csma, again.afsk.tx_level, again.callsign,
            again.beacon_enabled) == ("USB Audio CODEC", s.afsk.address, False, 0.5, "IU4AAJ", False)
    s = Settings()
    s.transport = "kiss"
    s.kiss.host, s.kiss.tcp_port = "tnc.local", 9001
    again = parse(*cli.dump_config_command(s))
    assert (again.kiss.host, again.kiss.tcp_port, again.kiss.address) == ("tnc.local", 9001, s.kiss.address)
    s = Settings()
    s.mesh.kind, s.mesh.address, s.mesh.port = "tcp", "10.0.0.5", 5000
    again = parse(*cli.dump_config_command(s))
    assert (again.mesh.address, again.mesh.port) == ("10.0.0.5", 5000)
