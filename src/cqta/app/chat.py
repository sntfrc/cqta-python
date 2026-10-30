"""CQta chat model: broadcast feed, stations, QSOs, beacon and keepalives. Runs on the dispatcher thread.

Output is delivered through callbacks carrying immutable snapshots; the GUI never touches this object directly.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from ..core import protocol as proto
from ..core.dispatcher import Dispatcher, Timer
from ..core.ipv4 import build_udp, bytes_to_ip
from ..core.protocol import CQtaError, CQtaPacket
from ..core.session import CQtaSession

KEEPALIVE_RANGE = (25, 35)
UDP_OVERHEAD = 20 + 8
MAX_BROADCAST_LINES = 500
NOTIFY_KEYS = ("direct", "broadcast", "file_start", "file", "cq", "mention")
NOTIFY_DEFAULTS = {"direct": True, "broadcast": False, "file_start": True, "file": True, "cq": False,
                   "mention": False}


@dataclass(frozen=True)
class Line:
    time: float
    kind: str  # "me" | "peer" | "system"
    label: str
    text: str
    cq_from: Optional[str] = None


@dataclass(frozen=True)
class ConvInfo:
    peer: str
    status: str
    closed: bool
    unread: int
    has_missing: bool


@dataclass(frozen=True)
class Station:
    callsign: str
    last_heard: float
    ip: Optional[str]


@dataclass
class _Conversation:
    peer: str
    lines: list[Line] = field(default_factory=list)
    closed: bool = False
    unread: int = 0
    status: str = ""
    session: Optional[CQtaSession] = None
    keepalive: Optional[Timer] = None


class ChatModel:
    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher
        self.callsign = proto.DEFAULT_CALLSIGN
        self.monitor = False
        self.notify = dict(NOTIFY_DEFAULTS)
        self.beacon_enabled = True
        self.beacon_interval = 120
        self.mtu = 200
        self.link_ready = False

        # Set by the owner.
        self.transmit: Callable[[bytes], None] = self._not_connected
        self.source_ip: Callable[[], bytes] = lambda: bytes(4)
        self.broadcast_ip: Callable[[], bytes] = lambda: bytes([10, 115, 255, 255])

        # Events (dispatcher thread).
        self.on_line: Callable[[Optional[str], Line], None] = lambda peer, line: None
        self.on_broadcast_cleared: Callable[[], None] = lambda: None
        self.on_conversation: Callable[[ConvInfo], None] = lambda info: None
        self.on_conversation_reset: Callable[[str], None] = lambda peer: None
        self.on_conversation_removed: Callable[[str], None] = lambda peer: None
        self.on_stations: Callable[[list[Station]], None] = lambda stations: None
        self.on_status: Callable[[str], None] = lambda m: None
        self.on_notice: Callable[[str], None] = lambda m: None
        self.on_notification: Callable[[str, str, str, Optional[str]], None] = lambda k, t, b, p: None
        self.on_beacon: Callable[[int, bool], None] = lambda countdown, tx: None
        self.on_callsign: Callable[[str], None] = lambda c: None

        self._stations: dict[str, float] = {}
        self._station_ips: dict[str, bytes] = {}
        self._convs: dict[str, _Conversation] = {}
        self._broadcast: list[Line] = []
        self._active_peer: Optional[str] = None
        self._countdown = self.beacon_interval
        self._beacon_tx_until = 0.0
        self._timer: Optional[Timer] = None

    @staticmethod
    def _not_connected(_: bytes) -> None:
        raise CQtaError("Not connected")

    def start(self) -> None:
        self._countdown = self.beacon_interval
        self._timer = self.dispatcher.call_every(1.0, self._tick)

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
        for c in self._convs.values():
            if c.keepalive:
                c.keepalive.cancel()

    # Properties

    @property
    def listen_only(self) -> bool:
        return self.callsign == proto.DEFAULT_CALLSIGN

    @property
    def max_udp_payload(self) -> int:
        return self.mtu - UDP_OVERHEAD

    def conversation_lines(self, peer: str) -> list[Line]:
        c = self._convs.get(peer)
        return list(c.lines) if c else []

    def snapshot(self) -> tuple[list[Line], list[ConvInfo], list[Station], dict[str, list[Line]]]:
        return (list(self._broadcast), [self._info(c) for c in self._convs.values()], self._stations_list(),
                {p: list(c.lines) for p, c in self._convs.items()})

    def station_ip(self, callsign: str) -> Optional[bytes]:
        return self._station_ips.get(callsign)

    # Settings

    def configure(self, callsign: str, monitor: bool, notify: dict[str, bool], beacon_enabled: bool,
                  beacon_interval: int) -> None:
        self.callsign = callsign if proto.is_valid_callsign(callsign) else proto.DEFAULT_CALLSIGN
        self.monitor = monitor
        self.notify = {**NOTIFY_DEFAULTS, **notify}
        self.beacon_enabled = beacon_enabled
        self.beacon_interval = min(max(beacon_interval, 15), 3600)
        self._countdown = self.beacon_interval

    def set_callsign(self, raw: str) -> Optional[str]:
        """Returns an error message, or None on success."""
        c = raw.strip().upper()
        if not proto.is_valid_callsign(c) or c == proto.QST:
            return "Enter a valid callsign."
        if c == self.callsign:
            return None
        if not self.listen_only and any(v.session is not None for v in self._convs.values()):
            return "Your callsign cannot be changed while QSOs are open. Close them first."
        if self.listen_only:
            self._forget_all()
        self.callsign = c
        self.on_callsign(c)
        self._countdown = self.beacon_interval
        self._beacon_now()
        return None

    def set_beacon(self, enabled: bool, interval: int) -> None:
        self.beacon_interval = min(max(interval, 15), 3600)
        turned_on = enabled and not self.beacon_enabled
        self.beacon_enabled = enabled
        self._countdown = self.beacon_interval
        if turned_on and not self.listen_only:
            self._transmit_beacon()

    def set_link(self, ready: bool, mtu: Optional[int] = None) -> None:
        if mtu is not None:
            self.mtu = mtu
        if ready != self.link_ready:
            self.link_ready = ready
            self._countdown = self.beacon_interval
            if ready:
                self._beacon_now()

    def set_active_peer(self, peer: Optional[str]) -> None:
        self._active_peer = peer
        c = self._convs.get(peer) if peer else None
        if c and c.unread:
            c.unread = 0
            self._publish(c)

    # Beacon

    def _beacon_now(self) -> None:
        if self.beacon_enabled and not self.listen_only and self.link_ready:
            if self._transmit_beacon():
                self._countdown = self.beacon_interval

    def _tick(self) -> None:
        if not (self.beacon_enabled and not self.listen_only and self.link_ready):
            return
        self._countdown -= 1
        if self._countdown <= 0:
            self._transmit_beacon()
            self._countdown = self.beacon_interval
        self.on_beacon(self._countdown, time.monotonic() < self._beacon_tx_until)

    def _transmit_beacon(self) -> bool:
        try:
            self._send(CQtaPacket(self.callsign, proto.QST, 0))
        except Exception as e:
            self.on_status(f"Beacon transmit failed: {e}")
            return False
        self._beacon_tx_until = time.monotonic() + 1.5
        self.on_beacon(self._countdown, True)
        return True

    # Transmit

    def _udp(self, payload: bytes) -> bytes:
        return build_udp(self.source_ip(), self.broadcast_ip(), proto.PORT, proto.PORT, payload)

    def _check_size(self, payload: bytes) -> None:
        if len(payload) > self.max_udp_payload:
            raise CQtaError(f"Packet is {len(payload)} bytes; the MTU allows at most "
                            f"{self.max_udp_payload} UDP payload bytes.")

    def _send(self, packet: CQtaPacket) -> None:
        payload = proto.encode(packet)
        self._check_size(payload)
        self.transmit(self._udp(payload))

    def send_broadcast(self, text: str) -> bool:
        if self.listen_only:
            self.on_status(f"{proto.DEFAULT_CALLSIGN} is listen-only; set a callsign to transmit.")
            return False
        try:
            self._send(CQtaPacket(self.callsign, proto.QST, 0, text=text))
        except Exception as e:
            self.on_status(f"Broadcast failed: {e}")
            self.on_notice(f"Broadcast failed: {e}")
            return False
        self._append_broadcast(self.callsign, text, None)
        return True

    def call_cq(self) -> bool:
        return self.send_broadcast("CQ")

    # QSOs

    def open_qso(self, raw_peer: str) -> Optional[str]:
        """Creates (or revives) the QSO; returns the normalised peer or None if invalid."""
        peer = raw_peer.strip().upper()
        if peer == proto.QST or peer == self.callsign or not proto.is_valid_callsign(peer):
            return None
        conv = self._convs.get(peer)
        if conv is None:
            conv = _Conversation(peer)
            self._convs[peer] = conv
            conv.session = CQtaSession(self.callsign, peer)
            self._update_status(conv)
            self._schedule_keepalive(conv)
        elif conv.closed or conv.session is None:
            conv.session = CQtaSession(self.callsign, peer)
            conv.closed = False
            self._update_status(conv)
            self._schedule_keepalive(conv)
        self._publish(conv)
        return peer

    def send_text(self, peer: str, text: str) -> bool:
        return self._send_new(peer, text, "Me")

    def request_missing(self, peer: str) -> None:
        c = self._convs.get(peer)
        if c and c.session and c.session.missing:
            self._send_new(peer, "", "Requesting missing packets")

    def restart_qso(self, peer: str, text: str = "") -> bool:
        conv = self._convs.get(peer)
        if conv is None or self.listen_only:
            return False
        packet = CQtaPacket(self.callsign, peer, 0, text=text)
        try:
            self._send(packet)
        except Exception as e:
            self.on_status(f"QSO restart failed: {e}")
            self.on_notice(f"QSO restart failed: {e}")
            return False
        session = conv.session or CQtaSession(self.callsign, peer)
        session.reset()
        session.record_sent(packet)
        conv.session = session
        conv.closed = False
        conv.lines.clear()
        self.on_conversation_reset(peer)
        if text:
            self._append_line(conv, Line(time.time(), "me", "Me", text))
        self._update_status(conv)
        self._schedule_keepalive(conv)
        return True

    def close_qso(self, peer: str) -> bool:
        conv = self._convs.get(peer)
        if conv is None:
            return True
        if not self.listen_only and conv.session is not None and not conv.closed:
            try:
                self._send(CQtaPacket(self.callsign, peer, conv.session.next_outgoing, text="QRT"))
            except Exception as e:
                self.on_status(f"Could not close QSO with {peer}: {e}")
                self.on_notice(f"QSO not closed: {e}")
                return False
            self.on_status(f"QRT sent to {peer}; QSO forgotten.")
        if conv.keepalive:
            conv.keepalive.cancel()
        del self._convs[peer]
        if self._active_peer == peer:
            self._active_peer = None
        self.on_conversation_removed(peer)
        return True

    def clear_qsos(self) -> None:
        self._forget_all()

    def clear_broadcast(self) -> None:
        self._broadcast.clear()
        self.on_broadcast_cleared()

    def clear_stations(self) -> None:
        self._stations.clear()
        self._station_ips.clear()
        self.on_stations([])

    def _forget_all(self) -> None:
        for peer in list(self._convs):
            c = self._convs.pop(peer)
            if c.keepalive:
                c.keepalive.cancel()
            self.on_conversation_removed(peer)
        self._active_peer = None

    def _send_keepalive(self, peer: str) -> None:
        self._send_new(peer, "", "")
        c = self._convs.get(peer)
        if c:
            self._schedule_keepalive(c)

    def _send_new(self, peer: str, text: str, label: str) -> bool:
        conv = self._convs.get(peer)
        if self.listen_only or conv is None or conv.session is None:
            return False
        session = conv.session
        packet = session.build_packet(text)
        try:
            self._send(packet)
        except Exception as e:
            self.on_status(f"Send to {peer} failed: {e}")
            if text:
                self.on_notice(f"Message not sent: {e}")
            self._update_status(conv)
            return False
        session.record_sent(packet)
        if text:
            self._append_line(conv, Line(time.time(), "me", label, text))
        self._update_status(conv)
        self._schedule_keepalive(conv)
        return True

    def _send_retransmission(self, conv: _Conversation, number: int) -> None:
        session = conv.session
        packet = session.build_retransmission(number) if session else None
        if packet is None:
            return
        payload = proto.encode(packet)
        if len(payload) > self.max_udp_payload:
            payload = proto.encode(session.sent[number])
        try:
            self.transmit(self._udp(payload))
        except Exception as e:
            self.on_status(f"Retransmission of {conv.peer} packet {number} failed: {e}")
            return
        self._append_line(conv, Line(time.time(), "system", "Resent", f"packet {number}"))

    def _schedule_keepalive(self, conv: _Conversation) -> None:
        if conv.keepalive:
            conv.keepalive.cancel()
            conv.keepalive = None
        if self.listen_only or conv.session is None:
            return
        delay = random.randint(*KEEPALIVE_RANGE)
        peer = conv.peer
        conv.keepalive = self.dispatcher.call_later(delay, lambda: self._send_keepalive(peer))

    def _update_status(self, conv: _Conversation) -> None:
        s = conv.session
        if s is None:
            conv.status = "QSO closed"
        else:
            missing = s.missing
            if missing:
                conv.status = (f"Next outgoing #{s.next_outgoing} · Missing from {conv.peer}: "
                               f"{proto.format_number_list(missing)}")
            else:
                conv.status = f"Next outgoing #{s.next_outgoing} · No known gaps"
        self._publish(conv)

    def _info(self, c: _Conversation) -> ConvInfo:
        return ConvInfo(c.peer, c.status, c.closed, c.unread, bool(c.session and c.session.missing))

    def _publish(self, c: _Conversation) -> None:
        self.on_conversation(self._info(c))

    def _append_line(self, conv: _Conversation, line: Line) -> None:
        conv.lines.append(line)
        self.on_line(conv.peer, line)

    # Receive

    def receive(self, data: bytes, ip: Optional[bytes] = None) -> None:
        """UDP payload received on port 37373."""
        if len(data) > self.max_udp_payload:
            self.on_status("Ignored oversized datagram.")
            return
        try:
            packet = proto.decode(data)
        except CQtaError as e:
            self.on_status(f"Ignored invalid packet: {e}")
            return
        if ip is not None and packet.source != self.callsign:
            self._station_ips[packet.source] = ip
        self._handle(packet)

    def _stations_list(self) -> list[Station]:
        return [Station(c, t, bytes_to_ip(self._station_ips[c]) if c in self._station_ips else None)
                for c, t in sorted(self._stations.items(), key=lambda kv: -kv[1])]

    def _handle(self, p: CQtaPacket) -> None:
        if p.source == self.callsign:
            return
        now = time.time()
        self._stations[p.source] = now
        self.on_stations(self._stations_list())

        if p.destination == proto.QST:
            if p.text:
                self._append_broadcast(p.source, p.text, None, now)
                mentioned = self.notify["mention"] and self.callsign.lower() in p.text.lower()
                is_cq = self.notify["cq"] and p.text.startswith("CQ")
                if self.notify["broadcast"] or mentioned or is_cq:
                    if self.notify["broadcast"] or (is_cq and not mentioned):
                        title = f"CQ from {p.source}" if is_cq else f"Broadcast from {p.source}"
                    else:
                        title = f"{p.source} mentioned {self.callsign}"
                    self.on_notification("broadcast", title, p.text, None)
            elif self.monitor:
                self._append_broadcast(p.source, "Beacon", "QST", now)
            return
        if self.monitor and p.text:
            self._append_broadcast(p.source, p.text, p.destination, now)
        if p.destination != self.callsign:
            return

        if self.open_qso(p.source) is None:
            return
        conv = self._convs[p.source]
        session = conv.session
        if session is None:
            return
        try:
            result = session.ingest(p)
        except CQtaError as e:
            self._append_line(conv, Line(now, "system", "Packet rejected", str(e)))
            return
        if result.restarted:
            self._append_line(conv, Line(now, "system", "QSO restarted",
                                         f"{p.source} started over; previous sequence and retransmission state cleared."))
        if result.is_new and p.text:
            self._append_line(conv, Line(now, "peer", p.source, p.text))
            if self._active_peer != conv.peer:
                conv.unread += 1
            if self.notify["direct"]:
                self.on_notification("direct", p.source, p.text, p.source)
        for n in result.unavailable:
            self._append_line(conv, Line(time.time(), "system", "Cannot retransmit",
                                         f"packet {n} is not in this run's send history"))
        for original in result.retransmissions:
            self._send_retransmission(conv, original.number)

        if p.text == "QRT":
            self._append_line(conv, Line(now, "system", "QSO closed", f"{p.source} sent QRT."))
            conv.session = None
            conv.closed = True
            if conv.keepalive:
                conv.keepalive.cancel()
                conv.keepalive = None
            conv.status = "QSO closed"
            self._publish(conv)
            self.on_status(f"{p.source} closed the QSO with QRT.")
            return
        self._update_status(conv)
        self._schedule_keepalive(conv)

    def _append_broadcast(self, source: str, text: str, destination: Optional[str],
                          at: Optional[float] = None) -> None:
        label = f"{source}→{destination}" if destination else source
        cq = source if destination is None and source != self.callsign and text.startswith("CQ") else None
        line = Line(at or time.time(), "me" if source == self.callsign else "peer", label, text, cq)
        self._broadcast.append(line)
        if len(self._broadcast) > MAX_BROADCAST_LINES:
            del self._broadcast[:len(self._broadcast) - MAX_BROADCAST_LINES]
        self.on_line(None, line)
