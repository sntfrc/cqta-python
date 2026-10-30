"""Owns the dispatcher, transport, chat, files, ping and the optional OS network interface.

Everything here runs on the dispatcher thread. The GUI calls `post` / `query` and receives results through the
`on_*` callbacks (also on the dispatcher thread; the Qt bridge turns them into signals).
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..core import engine as eng
from ..core import ipv4, protocol as proto
from ..core.dispatcher import Dispatcher
from ..transports.afsk import AFSKTransport
from ..transports.base import LinkState, Transport, TransportInfo
from ..transports.kiss import KissTransport
from ..transports.mesh import MeshTransport
from ..tun.client import TunBridge
from .chat import ChatModel
from .files import FilesModel
from .settings import Settings

PING_TIMEOUT = 60.0


@dataclass(frozen=True)
class PingEntry:
    key: int
    target: str
    seq: int
    state: str  # "pending" | "reply" | "timeout" | "failed"
    rtt_ms: Optional[float] = None
    detail: str = ""


@dataclass(frozen=True)
class NodeRow:
    num: int
    long_name: str
    short_name: str
    ip: str
    last_heard: Optional[float]


class Controller:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dispatcher = Dispatcher()
        self.chat = ChatModel(self.dispatcher)
        self.files = FilesModel(self.dispatcher)
        self.transport: Optional[Transport] = None
        self.bridge: Optional[TunBridge] = None
        self.tun_interface = ""

        self.on_link: Callable[[LinkState, str], None] = lambda s, d: None
        self.on_info: Callable[[Optional[TransportInfo]], None] = lambda i: None
        self.on_log: Callable[[str], None] = lambda m: None
        self.on_ping: Callable[[PingEntry], None] = lambda p: None
        self.on_nodes: Callable[[list[NodeRow]], None] = lambda n: None
        self.on_duty: Callable[[bool, bool], None] = lambda limited, override: None
        self.on_duty_failed: Callable[[str], None] = lambda m: None
        self.on_tun: Callable[[str, str], None] = lambda state, detail: None  # state: off|starting|on|error
        self.on_stats: Callable[[dict[str, Any]], None] = lambda s: None
        self._pings: dict[tuple[int, int], tuple[PingEntry, float]] = {}
        self._ping_ident = random.randint(1, 0xFFFF)
        self._ping_seq = 0
        self._ping_key = 0
        self._tun_gen = 0
        self._stats_timer = None

        c = self.chat
        c.source_ip = self._source_ip
        c.broadcast_ip = self._broadcast_ip
        c.transmit = self._transmit
        f = self.files
        f.source_ip = self._source_ip
        f.transmit = self._transmit
        f.resolve_callsign = c.station_ip
        f.display_name = self._display_name

    # Lifecycle

    def start(self) -> None:
        s = self.settings
        self.dispatcher.start()
        self.query(lambda: self._apply_settings(s))
        self.dispatcher.post(self._begin)

    def _begin(self) -> None:
        self.chat.start()
        self.files.start()
        self._stats_timer = self.dispatcher.call_every(1.0, self._tick)

    def shutdown(self) -> None:
        def stop() -> None:
            self._disconnect()
            self.chat.stop()
            self.files.stop()
            if self._stats_timer:
                self._stats_timer.cancel()

        try:
            self.query(stop, timeout=5)
        finally:
            self.dispatcher.stop()

    def post(self, fn: Callable[[], None]) -> None:
        self.dispatcher.post(fn)

    def query(self, fn: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Run `fn` on the dispatcher thread and return its result (call from other threads only)."""
        if self.dispatcher.in_thread():
            return fn()
        done = threading.Event()
        box: list[Any] = []

        def run() -> None:
            try:
                box.append(("ok", fn()))
            except BaseException as e:
                box.append(("err", e))
            done.set()

        self.dispatcher.post(run)
        if not done.wait(timeout):
            raise TimeoutError("The core did not respond")
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    # Settings

    def _apply_settings(self, s: Settings) -> None:
        self.settings = s
        self.chat.configure(s.callsign, s.monitor, s.notify, s.beacon_enabled, s.beacon_interval)
        self.files.set_server(s.tftp_server)
        if s.download_dir:
            self.files.set_folder(s.download_dir)

    def apply_settings(self, s: Settings) -> None:
        self.post(lambda: self._apply_settings(s))

    def sync_settings(self) -> Settings:
        """Copy the chat model's live values back into the settings and persist them (dispatcher thread)."""
        s, c = self.settings, self.chat
        s.callsign, s.monitor, s.notify = c.callsign, c.monitor, dict(c.notify)
        s.beacon_enabled, s.beacon_interval = c.beacon_enabled, c.beacon_interval
        s.tftp_server = self.files.server_enabled
        s.save()
        return s

    # Connection

    def connect(self) -> None:
        self._disconnect()
        s = self.settings
        try:
            if s.transport == "meshtastic":
                t: Transport = MeshTransport(self.dispatcher, s.mesh)
                t.on_duty_cycle = lambda limited, override: self.on_duty(limited, override)
                t.on_duty_cycle_failed = lambda m: self.on_duty_failed(m)
                t.on_nodes_changed = self._publish_nodes
            elif s.transport == "afsk":
                t = AFSKTransport(self.dispatcher, s.afsk)
            elif s.transport == "kiss":
                t = KissTransport(self.dispatcher, s.kiss)
            else:
                raise ValueError(f"Unknown transport {s.transport}")
            t.on_packet = self._on_packet
            t.on_state = self._on_state
            t.on_info = self._on_info
            t.log = self.on_log
            self.transport = t
            t.start()
        except Exception as e:
            self.transport = None
            self.on_log(f"Cannot connect: {e}")
            self.on_link(LinkState.STOPPED, str(e))

    def disconnect(self) -> None:
        self._disconnect()
        self.on_link(LinkState.STOPPED, "")

    def _disconnect(self) -> None:
        self._stop_tun()
        t, self.transport = self.transport, None
        if t is not None:
            t.stop()
        self.chat.set_link(False)
        self.files.link_ready = False
        self.on_info(None)
        self.on_nodes([])

    def _on_state(self, state: LinkState, detail: str) -> None:
        ready = state is LinkState.READY
        self.chat.set_link(ready)
        self.files.link_ready = ready
        self.on_link(state, detail)

    def _on_info(self, info: TransportInfo) -> None:
        self.chat.set_link(self.chat.link_ready, info.mtu)
        self.files.mtu = info.mtu
        self.on_info(info)
        if self.settings.system_interface:
            self._start_tun(info)

    def set_duty_cycle_override(self, enabled: bool) -> None:
        t = self.transport
        if isinstance(t, MeshTransport):
            if not t.set_duty_cycle_override(enabled):
                self.on_duty_failed("The node is not ready, or its region has no duty cycle limit")

    # Packet routing

    def _info(self) -> Optional[TransportInfo]:
        t = self.transport
        return t.info if t is not None and t.state is LinkState.READY else None

    def _source_ip(self) -> bytes:
        info = self._info()
        return info.my_ip if info else bytes(4)

    def _broadcast_ip(self) -> bytes:
        info = self._info()
        if info is None:
            raise proto.CQtaError("Not connected")
        return info.broadcast

    def _transmit(self, packet: bytes) -> None:
        t = self.transport
        if t is None or self._info() is None:
            raise proto.CQtaError("Not connected")
        t.send(packet)

    def _display_name(self, ip: str) -> str:
        for station in self.chat.snapshot()[2]:
            if station.ip == ip:
                return station.callsign
        return ip

    def _on_packet(self, packet: bytes) -> None:
        """An IPv4 packet arrived from the radio. Order mirrors the iOS app: CQta, TFTP, ICMP, then the OS."""
        d = ipv4.parse_udp(packet)
        if d is not None:
            if d.destination_port == proto.PORT:
                self.chat.receive(d.payload, d.source)
                return
            if self.files.handle(d):
                return
        else:
            echo = ipv4.parse_icmp_echo(packet)
            if echo is not None:
                if self._handle_icmp(packet, echo):
                    return
        if self.bridge is not None and self.tun_interface:
            self.bridge.write(packet)

    def _handle_icmp(self, packet: bytes, echo: ipv4.IcmpEcho) -> bool:
        info = self._info()
        if info is None:
            return True
        if echo.kind == 0:
            hit = self._pings.pop((echo.ident, echo.seq), None)
            if hit is None:
                return False
            entry, sent = hit
            rtt = (time.monotonic() - sent) * 1000
            self.on_ping(PingEntry(entry.key, entry.target, entry.seq, "reply", rtt))
            return True
        if echo.destination != info.my_ip:
            return False
        if self.tun_interface:
            return False  # the operating system answers
        reply = ipv4.icmp_reply(packet)
        if reply is not None and self.transport is not None:
            self.transport.send(reply)
        return True

    def _from_tun(self, packet: bytes) -> None:
        t = self.transport
        if t is not None and t.state is LinkState.READY:
            t.send(packet)

    # Ping

    def ping(self, target: str) -> None:
        info = self._info()
        self._ping_key += 1
        key = self._ping_key
        self._ping_seq = (self._ping_seq + 1) & 0xFFFF
        seq = self._ping_seq
        if info is None:
            self.on_ping(PingEntry(key, target, seq, "failed", detail="Not connected"))
            return
        dest = self.files.resolve(target)
        if dest is None:
            self.on_ping(PingEntry(key, target, seq, "failed", detail="Unknown address or callsign"))
            return
        pkt = ipv4.build_icmp(8, info.my_ip, dest, self._ping_ident, seq, bytes(range(32)))
        entry = PingEntry(key, target.strip(), seq, "pending")
        self._pings[(self._ping_ident, seq)] = (entry, time.monotonic())
        self.on_ping(entry)
        self._transmit(pkt)

    def _expire_pings(self) -> None:
        now = time.monotonic()
        for k, (entry, sent) in list(self._pings.items()):
            if now - sent > PING_TIMEOUT:
                del self._pings[k]
                self.on_ping(PingEntry(entry.key, entry.target, entry.seq, "timeout"))

    # Nodes

    def _publish_nodes(self) -> None:
        t = self.transport
        if not isinstance(t, MeshTransport):
            return
        rows = []
        for num, n in t.engine.nodes.items():
            rows.append(NodeRow(num, n.long_name or "", n.short_name or "", eng.node_ip(num),
                                t.engine.last_heard.get(num)))
        rows.sort(key=lambda r: (-(r.last_heard or 0), r.num))
        self.on_nodes(rows)

    # TUN

    def _start_tun(self, info: TransportInfo) -> None:
        if self.bridge is not None:
            return
        self._tun_gen += 1
        gen = self._tun_gen
        bridge = TunBridge()
        self.bridge = bridge
        bridge.on_packet = lambda p: self.dispatcher.post(lambda: self._from_tun(p))
        bridge.on_closed = lambda reason: self.dispatcher.post(lambda: self._tun_closed(gen, reason))
        self.on_tun("starting", "Waiting for administrator approval…")
        ip = ipv4.bytes_to_ip(info.my_ip)
        mask = ipv4.bytes_to_ip(info.netmask)

        def work() -> None:
            try:
                name = bridge.start(ip, mask, info.mtu)
            except Exception as e:
                self.dispatcher.post(lambda: self._tun_failed(gen, str(e) or type(e).__name__))
                return
            self.dispatcher.post(lambda: self._tun_ready(gen, name))

        threading.Thread(target=work, name="cqta-tun-start", daemon=True).start()

    def _tun_ready(self, gen: int, name: str) -> None:
        if gen != self._tun_gen:
            return
        self.tun_interface = name
        self.on_tun("on", name)

    def _tun_failed(self, gen: int, message: str) -> None:
        if gen != self._tun_gen:
            return
        self.bridge = None
        self.tun_interface = ""
        self.on_tun("error", message)
        self.on_log(f"Network interface: {message}")

    def _tun_closed(self, gen: int, reason: str) -> None:
        if gen != self._tun_gen:
            return
        self.bridge = None
        self.tun_interface = ""
        self.on_tun("error", reason)

    def _stop_tun(self) -> None:
        self._tun_gen += 1
        b, self.bridge = self.bridge, None
        self.tun_interface = ""
        if b is not None:
            threading.Thread(target=b.stop, daemon=True).start()
            self.on_tun("off", "")

    def set_system_interface(self, enabled: bool) -> None:
        self.settings.system_interface = enabled
        if not enabled:
            self._stop_tun()
        elif (info := self._info()) is not None:
            self._start_tun(info)

    # Periodic

    def _tick(self) -> None:
        self._expire_pings()
        t = self.transport
        if t is None:
            return
        stats: dict[str, Any] = {"tx": t.tx_packets, "rx": t.rx_packets}
        if isinstance(t, AFSKTransport):
            stats.update(level=t.input_level, busy=t.channel_busy, transmitting=t.transmitting, queued=t.queued)
        self.on_stats(stats)
