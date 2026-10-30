"""Transport-independent Meshtastic tunnel logic: config handshake, node table, IP <-> mesh translation.

Not thread-safe: call every method from the dispatcher thread.
"""

from __future__ import annotations

import enum
import random
import time
from typing import Callable, Optional

from . import ipv4, meshwire as mw

MTU = 200  # the Data payload limit is 233 bytes
SUBNET_PREFIX = (10, 115)
NETMASK = "255.255.0.0"
NETWORK = "10.115.0.0"
BROADCAST_IP = bytes([10, 115, 255, 255])


def node_ip(num: int) -> str:
    return f"{SUBNET_PREFIX[0]}.{SUBNET_PREFIX[1]}.{(num >> 8) & 0xFF}.{num & 0xFF}"


class MeshAddressing:
    """Node-number <-> IP mapping. Only the low 16 bits of the node number are used."""

    def __init__(self) -> None:
        self._low_to_node: dict[int, int] = {}

    def learn(self, num: int) -> None:
        self._low_to_node[num & 0xFFFF] = num

    def node_for_destination(self, ip: bytes) -> Optional[int]:
        if len(ip) != 4 or ip[0] != SUBNET_PREFIX[0] or ip[1] != SUBNET_PREFIX[1]:
            return None
        bits = (ip[2] << 8) | ip[3]
        if bits == 0xFFFF:
            return mw.BROADCAST_NODE_NUM
        return self._low_to_node.get(bits)

    @property
    def known_node_count(self) -> int:
        return len(self._low_to_node)


class State(enum.Enum):
    IDLE = "idle"
    CONFIGURING = "configuring"
    READY = "ready"


class RadioTunnelEngine:
    def __init__(self) -> None:
        self.send_to_radio: Callable[[bytes], None] = lambda _: None
        self.deliver_to_tunnel: Callable[[bytes], None] = lambda _: None
        self.on_ready: Callable[[int], None] = lambda _: None
        self.on_radio_rebooted: Callable[[], None] = lambda: None
        self.on_lora_config: Callable[[bool, bool], None] = lambda limited, override: None
        self.on_duty_cycle_change_failed: Callable[[str], None] = lambda _: None
        self.on_nodes_changed: Callable[[], None] = lambda: None
        self.log: Callable[[str], None] = lambda _: None

        self.state = State.IDLE
        self.my_node_num = 0
        self.addressing = MeshAddressing()
        self.nodes: dict[int, mw.NodeInfo] = {}
        self.last_heard: dict[int, float] = {}
        self.tx_packets = 0
        self.rx_packets = 0
        self.lora_config: Optional[bytes] = None
        self._pending_set_id: Optional[int] = None
        self._pending_lora: Optional[bytes] = None
        self._nonce = 0
        self._next_packet_id = random.randint(1, 0xFFFFFFFF)

    def start(self) -> None:
        """Begin the config download (ToRadio.want_config_id). Call after the link is up."""
        self.state = State.CONFIGURING
        self.my_node_num = 0
        self.nodes = {}
        self.lora_config = None
        self._pending_set_id = None
        self._nonce = random.randint(1, 0xFFFFFFFF)
        self.send_to_radio(mw.want_config(self._nonce))

    def stop(self) -> None:
        if self.state is not State.IDLE:
            self.send_to_radio(mw.disconnect())
        self.state = State.IDLE

    def _new_packet_id(self) -> int:
        self._next_packet_id = 1 if self._next_packet_id == 0xFFFFFFFF else self._next_packet_id + 1
        return self._next_packet_id

    def set_duty_cycle_override(self, enabled: bool) -> bool:
        lora = self.lora_config
        if (self.state is not State.READY or lora is None or not mw.is_duty_cycle_limited(lora)
                or self._pending_set_id is not None):
            return False
        pid = self._new_packet_id()
        self._pending_set_id = pid
        desired = mw.lora_config_with_override(lora, enabled)
        self._pending_lora = desired
        self.send_to_radio(mw.set_lora_config(desired, self.my_node_num, pid))
        return True

    @property
    def is_duty_cycle_change_pending(self) -> bool:
        return self._pending_set_id is not None

    def abandon_pending_duty_cycle_change(self) -> None:
        if self._pending_set_id is None:
            return
        self._pending_set_id = None
        self.on_duty_cycle_change_failed("The node did not confirm the duty cycle change")

    def _update_lora(self, lora: bytes) -> None:
        self.lora_config = lora
        self._pending_set_id = None
        self._pending_lora = None
        self.on_lora_config(mw.is_duty_cycle_limited(lora), mw.overrides_duty_cycle(lora))

    def send_heartbeat(self) -> None:
        self.send_to_radio(mw.heartbeat())

    def handle_from_radio(self, data: bytes) -> None:
        msg = mw.parse_from_radio(data)
        if isinstance(msg, mw.MyInfo):
            self.my_node_num = msg.node_num
        elif isinstance(msg, mw.NodeInfo):
            if msg.num != self.my_node_num:
                self.addressing.learn(msg.num)
                self.nodes[msg.num] = msg
                self.on_nodes_changed()
        elif isinstance(msg, mw.LoraConfigMsg):
            self._update_lora(msg.data)
        elif isinstance(msg, mw.ConfigComplete):
            if msg.id == self._nonce and self.state is State.CONFIGURING:
                self.state = State.READY
                self.on_ready(self.my_node_num)
        elif isinstance(msg, mw.MeshPacket):
            self._handle_mesh_packet(msg)
        elif isinstance(msg, mw.Rebooted):
            self.state = State.IDLE
            self.on_radio_rebooted()

    def _handle_mesh_packet(self, p: mw.MeshPacket) -> None:
        # Any traffic from a node tells us it exists (we only get NODEINFO occasionally).
        if p.from_ != 0 and p.from_ != self.my_node_num:
            self.addressing.learn(p.from_)
            self.last_heard[p.from_] = time.time()
            if p.from_ not in self.nodes:
                self.nodes[p.from_] = mw.NodeInfo(p.from_)
            self.on_nodes_changed()
        # Replies from the node we're attached to may carry from == 0 or our own number.
        if p.has_decoded and (p.from_ == self.my_node_num or p.from_ == 0):
            self._handle_local_packet(p)
        if not p.has_decoded or p.portnum != mw.PORTNUM_IP_TUNNEL_APP or p.from_ == self.my_node_num:
            return
        if ipv4.should_drop(p.payload):
            return
        self.rx_packets += 1
        self.deliver_to_tunnel(p.payload)

    def _handle_local_packet(self, p: mw.MeshPacket) -> None:
        if p.portnum == mw.PORTNUM_ADMIN_APP:
            lora = mw.lora_config_in_admin(p.payload)
            if lora is not None:
                self._update_lora(lora)
        elif p.portnum == mw.PORTNUM_ROUTING_APP:
            if self._pending_set_id is None or p.request_id != self._pending_set_id:
                return
            error = mw.routing_error(p.payload)
            if error == 0:
                if self._pending_lora is not None:
                    self._update_lora(self._pending_lora)
            else:
                self._pending_set_id = None
                self.on_duty_cycle_change_failed(f"The node rejected the duty cycle change (error {error})")

    def handle_outbound(self, ip_packet: bytes) -> None:
        """Forward an IP packet read from the tunnel into the mesh."""
        if self.state is not State.READY or ipv4.should_drop(ip_packet):
            return
        dst = ipv4.destination_of(ip_packet)
        node = self.addressing.node_for_destination(dst) if dst else None
        if node is None:
            return
        packet = mw.MeshPacket(to=node, id=self._new_packet_id(), want_ack=False,
                               portnum=mw.PORTNUM_IP_TUNNEL_APP, payload=ip_packet, has_decoded=True)
        self.tx_packets += 1
        self.send_to_radio(mw.to_radio(packet))
