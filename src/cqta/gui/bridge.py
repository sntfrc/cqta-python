"""Qt signal bridge: converts controller callbacks (dispatcher thread) into queued signals for the GUI thread."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from ..app.controller import Controller
from ..transports.base import LinkState


class CoreBridge(QObject):
    link = Signal(object, str)  # LinkState, detail
    info = Signal(object)  # TransportInfo | None
    log = Signal(str)
    ping = Signal(object)
    nodes = Signal(str, object)  # transport kind, rows
    duty = Signal(bool, bool, bool)  # limited, override, pending
    duty_failed = Signal(str)
    tun = Signal(str, str)
    stats = Signal(object)
    line = Signal(object, object)  # peer | None, Line
    broadcast_cleared = Signal()
    conversation = Signal(object)  # ConvInfo
    conversation_reset = Signal(str)
    conversation_removed = Signal(str)
    stations = Signal(object)
    status = Signal(str)
    notice = Signal(str)
    notification = Signal(str, str, str, object)
    beacon = Signal(int, bool)
    callsign = Signal(str)
    transfers = Signal(object)
    files = Signal(object)
    file_received = Signal(str, int)
    incoming_started = Signal(str, str)

    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self.ctrl = ctrl
        ctrl.on_link = self.link.emit
        ctrl.on_info = self.info.emit
        ctrl.on_log = self.log.emit
        ctrl.on_ping = self.ping.emit
        ctrl.on_nodes = self.nodes.emit
        ctrl.on_duty = self.duty.emit
        ctrl.on_duty_failed = self.duty_failed.emit
        ctrl.on_tun = self.tun.emit
        ctrl.on_stats = self.stats.emit
        c = ctrl.chat
        c.on_line = self.line.emit
        c.on_broadcast_cleared = self.broadcast_cleared.emit
        c.on_conversation = self.conversation.emit
        c.on_conversation_reset = self.conversation_reset.emit
        c.on_conversation_removed = self.conversation_removed.emit
        c.on_stations = self._stations_changed
        c.on_status = self.status.emit
        c.on_notice = self.notice.emit
        c.on_notification = self.notification.emit
        c.on_beacon = self.beacon.emit
        c.on_callsign = self.callsign.emit
        f = ctrl.files
        f.on_transfers = self.transfers.emit
        f.on_files = self.files.emit
        f.on_status = self.status.emit
        f.on_file_received = self.file_received.emit
        f.on_incoming_started = self.incoming_started.emit

    def _stations_changed(self, stations) -> None:
        self.stations.emit(stations)
        self.ctrl.publish_nodes()  # heard stations double as the node list on non-Meshtastic links

    def post(self, fn: Callable[[], None]) -> None:
        self.ctrl.post(fn)

    def query(self, fn: Callable[[], Any]) -> Any:
        return self.ctrl.query(fn)

    @staticmethod
    def state_text(state: LinkState) -> str:
        return {LinkState.STOPPED: "Disconnected", LinkState.CONNECTING: "Connecting",
                LinkState.READY: "Connected"}[state]
