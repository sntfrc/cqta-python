"""In-memory state for one directed CQta QSO."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .protocol import MAX_NUMBER_LIST_ITEMS, CQtaError, CQtaPacket


@dataclass
class ReceiveResult:
    is_new: bool
    restarted: bool
    retransmissions: list[CQtaPacket] = field(default_factory=list)
    unavailable: list[int] = field(default_factory=list)


class CQtaSession:
    def __init__(self, local_call: str, peer_call: str) -> None:
        self.local_call = local_call
        self.peer_call = peer_call
        self.next_outgoing = 0
        self.receive_start: Optional[int] = None
        self.received: set[int] = set()
        self.peer_empty: set[int] = set()
        self.sent: dict[int, CQtaPacket] = {}
        self.pending_empty_confirmations: set[int] = set()

    def reset(self) -> None:
        self.next_outgoing = 0
        self.receive_start = 0
        self.received.clear()
        self.peer_empty.clear()
        self.sent.clear()
        self.pending_empty_confirmations.clear()

    @property
    def missing(self) -> list[int]:
        if not self.received and not self.peer_empty:
            return []
        known = self.received | self.peer_empty
        highest = max(known)
        start = self.receive_start or 0
        if highest < start:
            return []
        return [n for n in range(start, highest + 1) if n not in known]

    def build_packet(self, text: str) -> CQtaPacket:
        no_content = set(self.pending_empty_confirmations)
        if not text:
            no_content.add(self.next_outgoing)
        return CQtaPacket(self.local_call, self.peer_call, self.next_outgoing,
                          requests=self.missing, no_content=sorted(no_content), text=text)

    def record_sent(self, packet: CQtaPacket) -> None:
        if (packet.source != self.local_call or packet.destination != self.peer_call
                or packet.number != self.next_outgoing):
            raise CQtaError("New outgoing packet does not match the QSO sequence.")
        self.sent[packet.number] = packet
        self.next_outgoing += 1

    def build_retransmission(self, number: int) -> Optional[CQtaPacket]:
        original = self.sent.get(number)
        if original is None:
            return None
        no_content = set(self.pending_empty_confirmations)
        if not original.text:
            no_content.add(number)
        return CQtaPacket(self.local_call, self.peer_call, original.number,
                          requests=self.missing, no_content=sorted(no_content), text=original.text)

    def ingest(self, packet: CQtaPacket) -> ReceiveResult:
        if packet.source != self.peer_call or packet.destination != self.local_call:
            raise CQtaError("Packet does not belong to this directed QSO.")
        restarted = packet.number == 0 and bool(self.received)
        if restarted:
            self.reset()

        if self.receive_start is None:
            self.receive_start = min([packet.number, *packet.no_content])

        known = self.received | self.peer_empty | set(packet.no_content)
        known.add(packet.number)
        start = self.receive_start
        missing_count = max(known) - start + 1 - sum(1 for n in known if n >= start)
        if missing_count > MAX_NUMBER_LIST_ITEMS:
            raise CQtaError(f"Packet would create more than {MAX_NUMBER_LIST_ITEMS} unresolved gaps.")

        is_new = packet.number not in self.received
        self.peer_empty.update(packet.no_content)
        self.received.update(packet.no_content)
        self.received.add(packet.number)
        if not packet.text:
            self.peer_empty.add(packet.number)

        self.pending_empty_confirmations &= set(packet.requests)
        result = ReceiveResult(is_new=is_new, restarted=restarted)
        for number in packet.requests:
            original = self.sent.get(number)
            if original is None:
                result.unavailable.append(number)
            elif not original.text:
                self.pending_empty_confirmations.add(number)
            elif number == 0:
                result.unavailable.append(number)
            else:
                result.retransmissions.append(original)
        return result
