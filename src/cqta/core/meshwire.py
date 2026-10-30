"""Meshtastic messages used by the tunnel (field numbers from meshtastic/protobufs)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from .protowire import ProtoReader, ProtoWriter

SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TO_RADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROM_RADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROM_NUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"

PORTNUM_ROUTING_APP = 5
PORTNUM_ADMIN_APP = 6
PORTNUM_IP_TUNNEL_APP = 33

BROADCAST_NODE_NUM = 0xFFFFFFFF

# LoRaConfig.Region values with a duty cycle: EU_433, EU_868, UA_433, UA_868.
DUTY_CYCLE_LIMITED_REGIONS = frozenset({2, 3, 14, 15})


@dataclass
class MeshPacket:
    from_: int = 0
    to: int = 0
    id: int = 0
    want_ack: bool = False
    portnum: int = 0
    payload: bytes = b""
    has_decoded: bool = False
    request_id: int = 0  # Data.request_id: the id of the packet this one answers


@dataclass
class NodeInfo:
    num: int
    long_name: Optional[str] = None
    short_name: Optional[str] = None


@dataclass
class MyInfo:
    node_num: int


@dataclass
class LoraConfigMsg:
    data: bytes


@dataclass
class ConfigComplete:
    id: int


class Rebooted:
    pass


class Other:
    pass


FromRadio = Union[MyInfo, NodeInfo, MeshPacket, LoraConfigMsg, ConfigComplete, Rebooted, Other]


def want_config(nonce: int) -> bytes:
    w = ProtoWriter()
    w.uint(3, nonce)
    return bytes(w.data)


def heartbeat() -> bytes:
    w = ProtoWriter()
    w.message(7, lambda _: None)
    return bytes(w.data)


def disconnect() -> bytes:
    w = ProtoWriter()
    w.bool(4, True)
    return bytes(w.data)


def to_radio(packet: MeshPacket) -> bytes:
    w = ProtoWriter()

    def build_packet(p: ProtoWriter) -> None:
        p.fixed32(2, packet.to)

        def build_data(d: ProtoWriter) -> None:
            d.uint(1, packet.portnum)
            d.bytes(2, packet.payload)

        p.message(4, build_data)
        p.fixed32(6, packet.id)
        p.bool(10, packet.want_ack)

    w.message(1, build_packet)
    return bytes(w.data)


def _u32(v) -> int:
    return int(v) & 0xFFFFFFFF if isinstance(v, int) else 0


def parse_from_radio(data: bytes) -> FromRadio:
    result: FromRadio = Other()
    for f, _, v in ProtoReader(data):
        if f == 2 and isinstance(v, bytes):
            result = parse_packet(v)
        elif f == 3 and isinstance(v, bytes):
            result = MyInfo(parse_my_info(v))
        elif f == 4 and isinstance(v, bytes):
            result = parse_node_info(v)
        elif f == 5 and isinstance(v, bytes):
            lora = lora_config_in_config(v)
            if lora is not None:
                result = LoraConfigMsg(lora)
        elif f == 7:
            result = ConfigComplete(_u32(v))
        elif f == 8:
            result = Rebooted()
    return result


def lora_region(lora: bytes) -> int:
    for f, _, v in ProtoReader(lora):
        if f == 7:
            return _u32(v)
    return 0


def is_duty_cycle_limited(lora: bytes) -> bool:
    return lora_region(lora) in DUTY_CYCLE_LIMITED_REGIONS


def overrides_duty_cycle(lora: bytes) -> bool:
    for f, _, v in ProtoReader(lora):
        if f == 12:
            return bool(v)
    return False


def lora_config_with_override(lora: bytes, enabled: bool) -> bytes:
    """Copy of `lora` with every field kept except `override_duty_cycle`, which is set to `enabled`."""
    w = ProtoWriter()
    for f, wt, v in ProtoReader(lora):
        if f != 12:
            w.raw_field(f, v, wt)
    w.bool(12, enabled)
    return bytes(w.data)


def _admin_to_radio(to: int, packet_id: int, want_ack: bool, build) -> bytes:
    admin = ProtoWriter()
    build(admin)
    return to_radio(MeshPacket(to=to, id=packet_id, want_ack=want_ack, portnum=PORTNUM_ADMIN_APP,
                               payload=bytes(admin.data), has_decoded=True))


def set_lora_config(lora: bytes, to: int, packet_id: int) -> bytes:
    """AdminMessage.set_config(Config{lora})"""
    return _admin_to_radio(to, packet_id, True, lambda a: a.message(34, lambda c: c.bytes(6, lora)))


def get_lora_config(to: int, packet_id: int) -> bytes:
    """AdminMessage.get_config_request(LORA_CONFIG)"""
    return _admin_to_radio(to, packet_id, False, lambda a: a.uint(5, 5))


def lora_config_in_admin(data: bytes) -> Optional[bytes]:
    for f, _, v in ProtoReader(data):
        if f == 6 and isinstance(v, bytes):
            return lora_config_in_config(v)
    return None


def lora_config_in_config(data: bytes) -> Optional[bytes]:
    for f, _, v in ProtoReader(data):
        if f == 6 and isinstance(v, bytes):
            return v
    return None


def routing_error(payload: bytes) -> int:
    """Routing.error_reason of a ROUTING_APP payload (0 = NONE, i.e. delivered / accepted)."""
    for f, _, v in ProtoReader(payload):
        if f == 3:
            return _u32(v)
    return 0


def parse_my_info(data: bytes) -> int:
    for f, _, v in ProtoReader(data):
        if f == 1:
            return _u32(v)
    return 0


def parse_node_info(data: bytes) -> NodeInfo:
    info = NodeInfo(0)
    for f, _, v in ProtoReader(data):
        if f == 1:
            info.num = _u32(v)
        elif f == 2 and isinstance(v, bytes):
            for uf, _, uv in ProtoReader(v):
                if uf == 2 and isinstance(uv, bytes):
                    info.long_name = uv.decode("utf-8", "replace")
                elif uf == 3 and isinstance(uv, bytes):
                    info.short_name = uv.decode("utf-8", "replace")
    return info


def parse_packet(data: bytes) -> MeshPacket:
    p = MeshPacket()
    for f, _, v in ProtoReader(data):
        if f == 1:
            p.from_ = _u32(v)
        elif f == 2:
            p.to = _u32(v)
        elif f == 6:
            p.id = _u32(v)
        elif f == 10:
            p.want_ack = bool(v)
        elif f == 4 and isinstance(v, bytes):
            p.has_decoded = True
            for df, _, dv in ProtoReader(v):
                if df == 1:
                    p.portnum = _u32(dv)
                elif df == 2 and isinstance(dv, bytes):
                    p.payload = dv
                elif df == 6:
                    p.request_id = _u32(dv)
    return p
