from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from ..core import engine as eng
from ..core.dispatcher import Dispatcher, Timer
from ..core.ipv4 import ip_to_bytes
from .base import Generation, LinkState, Reconnector, Transport, TransportInfo
from .mesh_links import BleLink, LinkError, MeshLink, SerialLink, TcpLink, TCP_PORT

HEARTBEAT_INTERVAL = 300.0
DUTY_CYCLE_TIMEOUT = 10.0
CONFIG_TIMEOUT = 30.0


@dataclass
class MeshConfig:
    kind: str = "serial"  # serial | ble | tcp
    address: str = ""  # port name, BLE address or host
    name: str = ""  # BLE device name, remembered so the selection can be shown before the next scan
    port: int = TCP_PORT  # TCP only

    def make_link(self) -> MeshLink:
        if self.kind == "serial":
            return SerialLink(self.address)
        if self.kind == "ble":
            return BleLink(self.address)
        if self.kind == "tcp":
            return TcpLink(self.address, self.port)
        raise LinkError(f"Unknown Meshtastic link type: {self.kind}")

    @property
    def description(self) -> str:
        return {"serial": "Serial", "ble": "Bluetooth", "tcp": "TCP"}.get(self.kind, self.kind) + f" {self.address}"


class MeshTransport(Transport):
    kind = "meshtastic"

    def __init__(self, dispatcher: Dispatcher, config: MeshConfig,
                 link_factory: Optional[Callable[[MeshConfig], MeshLink]] = None) -> None:
        super().__init__(dispatcher)
        self.config = config
        self.engine = eng.RadioTunnelEngine()
        self.engine.send_to_radio = self._to_radio
        self.engine.deliver_to_tunnel = self._deliver
        self.engine.on_ready = self._on_ready
        self.engine.on_radio_rebooted = self._on_rebooted
        self.engine.on_lora_config = self._on_lora
        self.engine.on_duty_cycle_change_failed = lambda m: self.on_duty_cycle_failed(m)
        self.engine.log = lambda m: self.log(m)
        self.on_duty_cycle: Callable[[bool, bool], None] = lambda limited, override: None
        self.on_duty_cycle_failed: Callable[[str], None] = lambda _: None
        self.on_nodes_changed: Callable[[], None] = lambda: None
        self.engine.on_nodes_changed = lambda: self.on_nodes_changed()

        self._link_factory = link_factory or (lambda c: c.make_link())
        self._link: Optional[MeshLink] = None
        self._gen = Generation()
        self._running = False
        self._reconnect = Reconnector(dispatcher, self._connect)
        self._heartbeat: Optional[Timer] = None
        self._duty_timer: Optional[Timer] = None
        self._config_timer: Optional[Timer] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._reconnect.reset()
        self._connect()

    def stop(self) -> None:
        self._running = False
        self._reconnect.cancel()
        self._drop_link(send_disconnect=True)
        self._set_state(LinkState.STOPPED, "")

    def send(self, ip_packet: bytes) -> None:
        if self.engine.state is eng.State.READY:
            self.engine.handle_outbound(ip_packet)
            self.tx_packets = self.engine.tx_packets

    def set_duty_cycle_override(self, enabled: bool) -> bool:
        if self._duty_timer:
            self._duty_timer.cancel()
        if not self.engine.set_duty_cycle_override(enabled):
            return False
        self._duty_timer = self.dispatcher.call_later(DUTY_CYCLE_TIMEOUT, self.engine.abandon_pending_duty_cycle_change)
        return True

    # Connection management

    def _connect(self) -> None:
        if not self._running:
            return
        self._drop_link(send_disconnect=False)
        gen = self._gen.bump()
        link = self._link_factory(self.config)
        self._link = link
        self._set_state(LinkState.CONNECTING, f"Connecting to {self.config.description}…")

        def on_message(data: bytes) -> None:
            self.dispatcher.post(lambda: self._on_message(gen, data))

        def on_closed(reason: str) -> None:
            self.dispatcher.post(lambda: self._on_closed(gen, reason))

        link.on_message = on_message
        link.on_closed = on_closed

        def work() -> None:
            try:
                link.open()
            except LinkError as e:
                self.dispatcher.post(lambda: self._on_closed(gen, str(e)))
                return
            except Exception as e:
                self.dispatcher.post(lambda: self._on_closed(gen, f"{type(e).__name__}: {e}"))
                return
            self.dispatcher.post(lambda: self._on_opened(gen))

        threading.Thread(target=work, name="cqta-mesh-open", daemon=True).start()

    def _on_opened(self, gen: int) -> None:
        if gen != self._gen.current or not self._running:
            return
        self._set_state(LinkState.CONNECTING, "Reading node configuration…")
        self.engine.start()
        self._config_timer = self.dispatcher.call_later(CONFIG_TIMEOUT, lambda: self._config_timeout(gen))

    def _config_timeout(self, gen: int) -> None:
        if gen == self._gen.current and self.engine.state is not eng.State.READY:
            self._fail("The node did not answer; is it a Meshtastic device?")

    def _on_message(self, gen: int, data: bytes) -> None:
        if gen == self._gen.current and self._running:
            self.engine.handle_from_radio(data)

    def _on_closed(self, gen: int, reason: str) -> None:
        if gen == self._gen.current and self._running:
            self._fail(reason)

    def _on_rebooted(self) -> None:
        if self._running:
            self._fail("The node rebooted")

    def _fail(self, reason: str) -> None:
        self._drop_link(send_disconnect=False)
        delay = self._reconnect.schedule()
        self._set_state(LinkState.CONNECTING, f"{reason}. Retrying in {delay:.0f} s…")
        self.log(f"Meshtastic: {reason}")

    def _drop_link(self, send_disconnect: bool) -> None:
        for t in (self._heartbeat, self._config_timer, self._duty_timer):
            if t:
                t.cancel()
        self._heartbeat = self._config_timer = self._duty_timer = None
        self._gen.bump()
        if send_disconnect and self._link is not None:
            self.engine.stop()  # writes ToRadio.disconnect while the link is still attached
        link, self._link = self._link, None
        if link is not None:
            threading.Thread(target=link.close, daemon=True).start()
        self.engine.state = eng.State.IDLE
        self.info = None

    def _to_radio(self, data: bytes) -> None:
        if self._link is not None:
            self._link.write(data)

    def _on_ready(self, node_num: int) -> None:
        if self._config_timer:
            self._config_timer.cancel()
        self._reconnect.reset()
        ip = ip_to_bytes(eng.node_ip(node_num))
        self._heartbeat = self.dispatcher.call_every(HEARTBEAT_INTERVAL, self.engine.send_heartbeat)
        self._set_info(TransportInfo(ip, ip_to_bytes(eng.NETWORK), ip_to_bytes(eng.NETMASK), eng.BROADCAST_IP,
                                     eng.MTU, f"Meshtastic {self.config.description}"))
        self._set_state(LinkState.READY, f"Connected, node !{node_num:08x}")

    def _on_lora(self, limited: bool, override: bool) -> None:
        self.on_duty_cycle(limited, override)
