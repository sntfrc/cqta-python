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
    s = parse("--tunnel", "10.1.2.3/16", "--kiss", ":9000", "--callsign", "n0call", "--beacon-interval", "60")
    assert (s.kiss.host, s.kiss.tcp_port, s.kiss.address, s.callsign) == ("127.0.0.1", 9000, "10.1.2.3/16", "N0CALL")
    assert s.kiss.kiss_port == 0
    assert s.beacon_enabled and s.beacon_interval == 60
    assert s.system_interface and not s.tftp_server
    s = parse("--tunnel")
    assert s.kiss.host == "saved.example" and s.callsign == "IU4AAJ" and s.system_interface


def test_mode_options():
    s = parse("--tunnel", "--meshtastic-tcp", "10.0.0.5:5000")
    assert (s.mesh.kind, s.mesh.address, s.mesh.port) == ("tcp", "10.0.0.5", 5000)
    s = parse("--tunnel", "--meshtastic-serial", "COM3", "--beacon-interval", "60", "--callsign", "iu4aaj")
    assert (s.mesh.kind, s.mesh.address, s.callsign, s.beacon_enabled) == ("serial", "COM3", "IU4AAJ", True)
    s = parse("--tunnel", "10.0.0.9", "--afsk", "--output-device", "USB", "--no-csma", "--tx-level", "0.5",
              "--callsign", "N0CALL", "--beacon-interval", "300")
    assert (s.afsk.output_name, s.afsk.input_name, s.afsk.csma, s.afsk.tx_level) == ("USB", "", False, 0.5)
    assert s.beacon_enabled and s.beacon_interval == 300


@pytest.mark.parametrize("argv", [
    ["--tunnel", "--kiss", ":1"],
    ["--tunnel", "10.1.1.1/16", "--kiss", "h", "--burst", "2"],
    ["--tunnel"],
    ["--tunnel", "10.1.1.1/16", "--kiss", "h:70000"],
    ["--tunnel", "nonsense", "--kiss", "h"],
    ["--tunnel", "--kiss", "h"],
    ["--tunnel", "10.1.1.1/16", "--kiss", "h", "--beacon-interval", "60"],
    ["--tunnel", "10.1.1.1/16", "--kiss", "h", "--callsign", "N0CALL"],
    ["--tunnel", "--callsign", "N0CALL", "--beacon-interval", "60"],
    ["--tunnel", "--beacon-interval", "60"],
    ["--tunnel", "10.1.1.1/16", "--meshtastic-tcp", "h", "--callsign", "N0CALL"],
    ["--tunnel"],
    ["--tunnel", "10.1.1.1/16", "--kiss", "h", "--afsk"],
])
def test_usage_errors(argv, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "saved_settings_path", lambda: tmp_path / "none.json")
    with pytest.raises(SystemExit) as e:
        parse(*argv)
    assert e.value.code == 2


def test_refuses_without_rights(monkeypatch, capsys):
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    assert cli.run_tunnel(parse("--tunnel", "10.1.1.1/16", "--kiss", "h")) == 1
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
    settings = parse("--tunnel", "10.9.9.9/24", "--kiss", f"127.0.0.1:{server.getsockname()[1]}")
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
    assert (again.afsk.input_name, again.afsk.address, again.afsk.csma, again.afsk.tx_level,
            again.beacon_enabled) == ("USB Audio CODEC", s.afsk.address, False, 0.5, False)
    s = Settings()
    s.transport = "kiss"
    s.kiss.host, s.kiss.tcp_port = "tnc.local", 9001
    again = parse(*cli.dump_config_command(s))
    assert (again.kiss.host, again.kiss.tcp_port, again.kiss.address) == ("tnc.local", 9001, s.kiss.address)
    s = Settings()
    s.mesh.kind, s.mesh.address, s.mesh.port = "tcp", "10.0.0.5", 5000
    again = parse(*cli.dump_config_command(s))
    assert (again.mesh.address, again.mesh.port) == ("10.0.0.5", 5000)


def test_defaults_for_omitted_values(monkeypatch):
    s = parse("--tunnel", "10.1.2.3/16", "--kiss")
    assert (s.kiss.host, s.kiss.tcp_port, s.kiss.kiss_port) == ("127.0.0.1", 8001, 0)
    s = parse("--tunnel", "10.1.2.3/16", "--kiss", "tnc.local/2")
    assert (s.kiss.host, s.kiss.tcp_port, s.kiss.kiss_port) == ("tnc.local", 8001, 2)
    s = parse("--tunnel", "10.1.2.3/16", "--kiss", ":9000")
    assert (s.kiss.host, s.kiss.tcp_port) == ("127.0.0.1", 9000)
    s = parse("--tunnel", "--meshtastic-tcp")
    assert (s.mesh.kind, s.mesh.address, s.mesh.port) == ("tcp", "127.0.0.1", 4403)
    monkeypatch.setattr(cli, "default_serial_port", lambda: "/dev/ttyACM0")
    s = parse("--tunnel", "--meshtastic-serial")
    assert (s.mesh.kind, s.mesh.address) == ("serial", "/dev/ttyACM0")
    monkeypatch.setattr(cli, "default_ble_device", lambda: "AA:BB")
    s = parse("--tunnel", "--meshtastic-ble")
    assert (s.mesh.kind, s.mesh.address) == ("ble", "AA:BB")


def test_default_serial_port_order(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    names = {"/dev/ttyACM*": ["/dev/ttyACM1", "/dev/ttyACM0"], "/dev/ttyUSB*": ["/dev/ttyUSB0"], "/dev/tty.usb*": []}
    import glob
    monkeypatch.setattr(glob, "glob", lambda pattern: list(names[pattern]))
    assert cli.default_serial_port() == "/dev/ttyACM0"
    names["/dev/ttyACM*"] = []
    assert cli.default_serial_port() == "/dev/ttyUSB0"
    names["/dev/ttyUSB*"] = []
    with pytest.raises(cli.DetectionError):
        cli.default_serial_port()


@pytest.mark.parametrize("argv", [
    ["--list", "serial", "--tunnel"], ["--dump-config", "--kiss"], ["--list", "serial", "--dump-config"],
    ["--dump-config", "--tunnel", "10.1.1.1/16"],
])
def test_standalone_actions_reject_other_options(argv):
    with pytest.raises(SystemExit) as e:
        cli._main(argv)
    assert e.value.code == 2
