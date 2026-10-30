"""Command-line mode: `cqta --tunnel` runs without a GUI and keeps an operating-system tunnel up until terminated.

Nothing here imports Qt. The command line never asks for administrator rights; it needs to be started with them.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .app.settings import Settings
from .core import protocol as proto
from .core.ipv4 import bytes_to_ip, parse_address
from .transports.base import LinkState
from .tun import elevate

MODES = ("meshtastic", "kiss", "afsk")
MODE_NAMES = {"meshtastic": "Meshtastic", "kiss": "KISS TNC", "afsk": "AFSK modem", "network": "Network interface"}

DESCRIPTION = """\
CQta - amateur radio chat, QSO, ping and file transfer over Meshtastic, KISS TNCs and AFSK audio.

Started without arguments, CQta opens its graphical interface. With --tunnel ADDRESS it runs without any GUI and keeps an
operating-system network interface (a TUN/TAP device) up, bridged to the chosen radio link, until it is terminated
with Ctrl+C (or SIGTERM), then tears everything down.

The command line never asks for administrator rights: creating the interface needs them, so start CQta as root
(sudo cqta --tunnel ...) on macOS and Linux, or from a terminal started with "Run as administrator" on Windows. If the
rights are missing, CQta says so and exits with an error."""

EPILOG = """\
connection selection:
  cqta --tunnel
      Reconnect with exactly the connection used last in the GUI (all its settings). Fails if there is none.
  cqta --tunnel [ADDRESS] --mode MODE --callsign CALL [mode options]
      Use the given connection for this session only. Nothing is remembered, and every option that is not given takes its default value (never the one saved by the GUI). --callsign is required. ADDRESS (a.b.c.d or a.b.c.d/prefix, prefix 1-30) is the IPv4 address of this station and is required for kiss and afsk; with meshtastic the address comes from the node and must not be given. Options of other modes are rejected.
  The Network interface mode of the GUI cannot be used here: it is a plain UDP transport and has no tunnel.

defaults:
  No beacons are sent unless --beacon-interval is given. KISS: 127.0.0.1 port 8001 channel 0 MTU 256. AFSK: system default audio devices, level 0.8, TX delay 500 ms, tail 60 ms, CSMA on, hold-off 600 ms, jitter 600 ms, 4 frames per transmission.

examples:
  cqta --dump-config       (prints the --tunnel command line equivalent to the GUI's current connection)
  sudo cqta --tunnel
  sudo cqta --tunnel --mode meshtastic --meshtastic-serial /dev/ttyUSB0 --callsign IU4AAJ --beacon-interval 120
  sudo cqta --tunnel --mode meshtastic --meshtastic-tcp 192.168.1.50 --callsign IU4AAJ
  sudo cqta --tunnel 44.130.1.5/16 --mode kiss --kiss-host 127.0.0.1 --kiss-port 8001 --callsign IU4AAJ
  sudo cqta --tunnel 10.115.4.7/16 --mode afsk --input-device "USB Audio CODEC" --output-device "USB Audio CODEC" --callsign IU4AAJ
  cqta --list serial          (find the names to use with the options above)

exit status: 0 after a normal stop, 1 if the tunnel could not be created or failed, 2 for a usage error."""


def _ranged(lo: int, hi: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{text}' is not a whole number") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"{value} is out of range ({lo}-{hi})")
        return value

    return parse


def _level(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number") from None
    if not 0.05 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"{value} is out of range (0.05-1.0)")
    return value


def _address(text: str) -> str:
    if parse_address(text) is None:
        raise argparse.ArgumentTypeError(f"'{text}' is not a valid address; use a.b.c.d or a.b.c.d/prefix (prefix 1-30)")
    return text.strip()


def _callsign(text: str) -> str:
    call = text.strip().upper()
    if not proto.is_valid_callsign(call) or call == proto.QST:
        raise argparse.ArgumentTypeError(f"'{text}' is not a valid callsign")
    return call


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cqta", description=DESCRIPTION, epilog=EPILOG, add_help=False,
        formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(prog, max_help_position=40, width=100000))
    g = p.add_argument_group("general")
    g.add_argument("-h", "--help", action="help", help="show this help and exit")
    g.add_argument("-V", "--version", action="version", version=f"cqta {__version__}", help="show the version and exit")
    g.add_argument("--tunnel", nargs="?", const=True, default=None, type=_address, metavar="ADDRESS",
                   help="run without a GUI and keep an operating-system tunnel up (needs administrator rights). ADDRESS is this station's IPv4 address, a.b.c.d[/prefix]: required with --mode kiss and --mode afsk, not allowed with --mode meshtastic (the node decides it); omit everything to reuse the GUI's last connection")
    g.add_argument("--list", choices=("serial", "ble", "audio", "interfaces"), metavar="WHAT",
                   help="print the available serial ports, Bluetooth (ble) Meshtastic nodes, audio devices or network interfaces, and exit")
    g.add_argument("--dump-config", action="store_true",
                   help="print the command line that reproduces the connection currently chosen in the GUI, and exit")
    g.add_argument("--mode", choices=MODES, help="connection type; selects session-only settings (see below)")
    g.add_argument("--callsign", type=_callsign, metavar="CALL", help="callsign used for the CQta chat protocol (required with --mode)")
    g.add_argument("--beacon-interval", type=_ranged(15, 3600), metavar="SECONDS",
                   help="send a beacon every SECONDS (15-3600); without this option no beacons are sent")

    m = p.add_argument_group("meshtastic mode (--mode meshtastic; exactly one of these is required)")
    x = m.add_mutually_exclusive_group()
    x.add_argument("--meshtastic-serial", metavar="PORT", help="serial port of the node, e.g. /dev/ttyUSB0 or COM3")
    x.add_argument("--meshtastic-ble", metavar="ADDRESS", help="Bluetooth LE address of the node (a UUID on macOS); see --list ble")
    x.add_argument("--meshtastic-tcp", metavar="HOST[:PORT]", help="network address of the node (default port 4403)")

    k = p.add_argument_group("kiss mode (--mode kiss): KISS TNC over TCP, e.g. Direwolf")
    k.add_argument("--kiss-host", metavar="HOST", help="TNC host name or address (default 127.0.0.1)")
    k.add_argument("--kiss-port", type=_ranged(1, 65535), metavar="PORT", help="TNC TCP port (default 8001)")
    k.add_argument("--kiss-channel", type=_ranged(0, 15), metavar="N", help="KISS channel/port number (0-15, default 0)")
    k.add_argument("--kiss-mtu", type=_ranged(68, 1500), metavar="BYTES", help="packet size limit (68-1500, default 256)")

    a = p.add_argument_group("afsk mode (--mode afsk): 1200 baud AFSK modem through a sound card")
    a.add_argument("--input-device", dest="afsk_input", metavar="NAME", help="input (receive) audio device name (default: system default); see --list audio")
    a.add_argument("--input-api", dest="afsk_input_api", metavar="API", help="audio API of the input device, when the name is ambiguous")
    a.add_argument("--output-device", dest="afsk_output", metavar="NAME", help="output (transmit) audio device name (default: system default)")
    a.add_argument("--output-api", dest="afsk_output_api", metavar="API", help="audio API of the output device")
    a.add_argument("--tx-level", dest="afsk_tx_level", type=_level, metavar="LEVEL", help="transmit audio level (0.05-1.0, default 0.8)")
    a.add_argument("--tx-delay", dest="afsk_tx_delay", type=_ranged(0, 3000), metavar="MS", help="key-up lead-in before each transmission (0-3000, default 500)")
    a.add_argument("--tx-tail", dest="afsk_tx_tail", type=_ranged(0, 1000), metavar="MS", help="tail after each transmission (0-1000, default 60)")
    a.add_argument("--no-csma", dest="afsk_csma", action="store_const", const=False, help="transmit without waiting for a quiet channel")
    a.add_argument("--holdoff", dest="afsk_holdoff", type=_ranged(0, 5000), metavar="MS", help="quiet time required before transmitting (0-5000, default 600)")
    a.add_argument("--jitter", dest="afsk_jitter", type=_ranged(0, 5000), metavar="MS", help="random extra hold-off (0-5000, default 600)")
    a.add_argument("--burst", dest="afsk_burst", type=_ranged(1, 16), metavar="N", help="frames per transmission (1-16, default 4)")
    return p


def _given(parser: argparse.ArgumentParser, args: argparse.Namespace, prefix: str) -> list[str]:
    flags = {a.dest: a.option_strings[-1] for a in parser._actions if a.option_strings}
    return [flags[k] for k, v in vars(args).items() if k.startswith(prefix) and v is not None]


def saved_settings_path() -> Path:
    """The GUI's settings file; under sudo that is the invoking user's, not root's."""
    path = Settings.path()
    sudo_user = os.environ.get("SUDO_USER")
    if os.name == "posix" and os.geteuid() == 0 and sudo_user:
        try:
            import pwd

            home = Path(pwd.getpwnam(sudo_user).pw_dir)
        except (ImportError, KeyError):
            return path
        base = home / "Library" / "Application Support" if sys.platform == "darwin" else home / ".config"
        return base / "cqta" / "settings.json"
    return path


def describe(s: Settings) -> str:
    if s.transport == "meshtastic":
        c = s.mesh
        target = f"{c.address}:{c.port}" if c.kind == "tcp" else c.address
        return f"Meshtastic {c.kind} {target}"
    if s.transport == "kiss":
        return f"KISS TNC {s.kiss.host}:{s.kiss.tcp_port}"
    if s.transport == "afsk":
        return f"AFSK modem (in: {s.afsk.input_name or 'default'}, out: {s.afsk.output_name or 'default'})"
    return MODE_NAMES.get(s.transport, s.transport)


def build_settings(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Settings:
    mode_options = {m: _given(parser, args, m + "_") for m in MODES}
    address = args.tunnel if isinstance(args.tunnel, str) else None
    if args.mode is None:
        stray = [o for opts in mode_options.values() for o in opts]
        if stray:
            parser.error(f"{', '.join(stray)} need --mode to be given")
        if address:
            parser.error("the --tunnel address needs --mode to be given")
        settings = Settings.load(saved_settings_path())
        source = "saved"
    else:
        for m, opts in mode_options.items():
            if m != args.mode and opts:
                parser.error(f"{', '.join(opts)} cannot be used with --mode {args.mode}")
        if not args.callsign:
            parser.error("--callsign is required with --mode")
        if args.mode == "meshtastic" and address:
            parser.error("--mode meshtastic takes its address from the node; do not give one to --tunnel")
        if args.mode != "meshtastic" and not address:
            parser.error(f"--mode {args.mode} needs this station's address: --tunnel a.b.c.d[/prefix]")
        settings = Settings()
        settings.transport = args.mode
        settings.beacon_enabled = False
        source = "given"
        if args.mode == "meshtastic":
            c = settings.mesh
            if args.meshtastic_serial:
                c.kind, c.address = "serial", args.meshtastic_serial
            elif args.meshtastic_ble:
                c.kind, c.address = "ble", args.meshtastic_ble
            elif args.meshtastic_tcp:
                host, _, port = args.meshtastic_tcp.rpartition(":")
                if host and port.isdigit():
                    c.address, c.port = host, _ranged(1, 65535)(port)
                else:
                    c.address = args.meshtastic_tcp
                c.kind = "tcp"
            else:
                parser.error("--mode meshtastic needs --meshtastic-serial, --meshtastic-ble or --meshtastic-tcp")
        elif args.mode == "kiss":
            c = settings.kiss
            c.host = args.kiss_host or c.host
            c.tcp_port = args.kiss_port or c.tcp_port
            c.kiss_port = c.kiss_port if args.kiss_channel is None else args.kiss_channel
            c.address = address
            c.mtu = args.kiss_mtu or c.mtu
        else:
            c = settings.afsk
            c.input_name = args.afsk_input or ""
            c.input_api = args.afsk_input_api or ""
            c.output_name = args.afsk_output or ""
            c.output_api = args.afsk_output_api or ""
            c.address = address
            c.tx_level = args.afsk_tx_level or c.tx_level
            c.tx_delay_ms = c.tx_delay_ms if args.afsk_tx_delay is None else args.afsk_tx_delay
            c.tx_tail_ms = c.tx_tail_ms if args.afsk_tx_tail is None else args.afsk_tx_tail
            c.csma = c.csma if args.afsk_csma is None else args.afsk_csma
            c.clear_holdoff_ms = c.clear_holdoff_ms if args.afsk_holdoff is None else args.afsk_holdoff
            c.clear_jitter_ms = c.clear_jitter_ms if args.afsk_jitter is None else args.afsk_jitter
            c.max_burst_frames = args.afsk_burst or c.max_burst_frames
    if args.callsign:
        settings.callsign = args.callsign
    if args.beacon_interval:
        settings.beacon_interval = args.beacon_interval
        settings.beacon_enabled = True

    if settings.transport == "network":
        parser.error("the last used connection is the Network interface, which has no tunnel; choose one with --mode")
    if settings.transport == "meshtastic" and not settings.mesh.address:
        if source == "saved":
            parser.error("there is no saved Meshtastic device; connect once in the GUI or use --mode meshtastic ...")
    # The operating system handles files, ping and everything else over the tunnel
    settings.system_interface = True
    settings.tftp_server = False
    return settings


def dump_config_command(s: Settings) -> list[str]:
    """Arguments that make `--tunnel --mode ...` reproduce `s`; options at their default value are left out."""
    from .transports.mesh_links import TCP_PORT

    d = Settings()
    out = ["--tunnel"]
    if s.transport in ("kiss", "afsk"):
        out.append(getattr(s, s.transport).address)
    out += ["--mode", s.transport]
    if s.transport == "meshtastic":
        c = s.mesh
        if c.kind == "serial":
            out += ["--meshtastic-serial", c.address]
        elif c.kind == "ble":
            out += ["--meshtastic-ble", c.address]
        else:
            out += ["--meshtastic-tcp", c.address if c.port == TCP_PORT else f"{c.address}:{c.port}"]
    elif s.transport == "kiss":
        c, k = s.kiss, d.kiss
        out += ["--kiss-host", c.host, "--kiss-port", str(c.tcp_port)] if (c.host, c.tcp_port) != (k.host, k.tcp_port) else []
        out += ["--kiss-channel", str(c.kiss_port)] if c.kiss_port != k.kiss_port else []
        out += ["--kiss-mtu", str(c.mtu)] if c.mtu != k.mtu else []
    elif s.transport == "afsk":
        c, a = s.afsk, d.afsk
        for flag, value in (("--input-device", c.input_name), ("--input-api", c.input_api),
                            ("--output-device", c.output_name), ("--output-api", c.output_api)):
            if value:
                out += [flag, value]
        for flag, value, default in (("--tx-level", c.tx_level, a.tx_level),
                                     ("--tx-delay", c.tx_delay_ms, a.tx_delay_ms),
                                     ("--tx-tail", c.tx_tail_ms, a.tx_tail_ms),
                                     ("--holdoff", c.clear_holdoff_ms, a.clear_holdoff_ms),
                                     ("--jitter", c.clear_jitter_ms, a.clear_jitter_ms),
                                     ("--burst", c.max_burst_frames, a.max_burst_frames)):
            if value != default:
                out += [flag, f"{value:g}" if isinstance(value, float) else str(value)]
        if not c.csma:
            out.append("--no-csma")
    out += ["--callsign", s.callsign]
    if s.beacon_enabled:
        out += ["--beacon-interval", str(s.beacon_interval)]
    return out


def print_dump_config() -> int:
    s = Settings.load(saved_settings_path())
    if s.transport == "network":
        print("The current connection is the Network interface, which has no tunnel and cannot be started "
              "from the command line.", file=sys.stderr)
        return 1
    if s.transport == "meshtastic" and not s.mesh.address:
        print("No Meshtastic device has been chosen in the GUI yet.", file=sys.stderr)
        return 1
    args = dump_config_command(s)
    if sys.platform == "win32":
        import subprocess

        print(subprocess.list2cmdline(["cqta", *args]))
    else:
        import shlex

        print("sudo " + " ".join(shlex.quote(a) for a in ["cqta", *args]))
    return 0


def attach_console() -> None:
    """The Windows executable has no console of its own; use the one it was started from, if any."""
    if sys.platform != "win32" or (sys.stdout is not None and sys.stderr is not None):
        return
    import ctypes

    if not ctypes.windll.kernel32.AttachConsole(-1):
        return
    try:
        if sys.stdout is None:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except OSError:
        pass


def _say(message: str, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", file=stream, flush=True)


def list_things(what: str) -> int:
    try:
        if what == "serial":
            from .transports.mesh_links import list_serial_ports

            rows = [(d.address, d.name) for d in list_serial_ports()]
        elif what == "audio":
            from .transports import audio

            ins, outs = audio.list_devices()
            print("Input devices (use with --input-device / --input-api):")
            for d in ins:
                print(f"  {d.name}  [{d.host_api}]")
            print("Output devices (use with --output-device / --output-api):")
            for d in outs:
                print(f"  {d.name}  [{d.host_api}]")
            return 0
        elif what == "interfaces":
            from .transports.network import list_interfaces

            rows = [(i.name, f"{i.address}/{i.prefix}") for i in list_interfaces()]
        else:
            rows = _scan_ble()
    except Exception as e:
        print(f"Cannot list {what}: {e}", file=sys.stderr)
        return 1
    if not rows:
        print("None found.")
    for address, name in rows:
        print(f"{address}  {name}" if name and name != address else address)
    return 0


def _scan_ble(seconds: float = 6.0) -> list[tuple[str, str]]:
    from .transports.mesh_links import BleScanner

    found: dict[str, str] = {}
    errors: list[str] = []
    scanner = BleScanner(lambda address, name, rssi: found.__setitem__(address, name), errors.append)
    print(f"Scanning for Bluetooth Meshtastic nodes for {seconds:.0f} s…", file=sys.stderr)
    scanner.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not errors:  # a failed scan ends early
        time.sleep(0.2)
    scanner.stop()
    if errors and not found:
        raise RuntimeError(errors[0])
    return sorted(found.items(), key=lambda kv: kv[1])


def run_tunnel(settings: Settings) -> int:
    from .app.controller import Controller

    if not elevate.is_elevated():
        _say("Creating the tunnel needs administrator rights. " + elevate.required_rights_hint(), error=True)
        return 1
    elevate.prompt_allowed = False

    ctrl = Controller(settings)
    stop = threading.Event()
    failure: list[str] = []
    last_status = [""]

    def fail(message: str) -> None:
        if not stop.is_set():
            failure.append(message)
            stop.set()

    def on_link(state: LinkState, detail: str) -> None:
        if stop.is_set():
            return
        if state is LinkState.STOPPED:
            fail(detail or "The connection was closed")
        elif detail and detail != last_status[0]:
            last_status[0] = detail
            _say(detail)

    def on_tun(state: str, detail: str) -> None:
        if state == "starting":
            _say("Creating the tunnel interface…")
        elif state == "error":
            fail(f"Tunnel failed: {detail}")
        elif state == "on":
            info = ctrl.transport.info if ctrl.transport is not None else None
            where = f" with address {bytes_to_ip(info.my_ip)}/{bytes_to_ip(info.netmask)}" if info else ""
            _say(f"Tunnel up: interface {detail}{where}. Press Ctrl+C to stop.")

    ctrl.on_link = on_link
    ctrl.on_tun = on_tun
    ctrl.on_log = _say

    for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: stop.set())

    _say(f"Starting: {describe(settings)} as {settings.callsign}")
    ctrl.start()
    ctrl.post(ctrl.connect)
    while not stop.wait(0.5):  # a timeout keeps Ctrl+C responsive on Windows
        pass
    _say("Stopping…")
    ctrl.shutdown()
    if failure:
        _say(failure[0], error=True)
        return 1
    _say("Stopped.")
    return 0


def main(argv: list[str]) -> int:
    attach_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dump_config:
        return print_dump_config()
    if args.list:
        return list_things(args.list)
    if not args.tunnel:
        parser.error("nothing to do: use --tunnel to run without the GUI, --list WHAT, --dump-config, or no arguments for the GUI")
    return run_tunnel(build_settings(args, parser))
