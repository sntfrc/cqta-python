"""CQta text protocol: ``SOURCE>DESTINATION NUMBER{REQUESTS}[NO-CONTENT]:TEXT`` (see cqta-python/PROTOCOL.md)."""

from __future__ import annotations

from dataclasses import dataclass, field

PORT = 37373
MAX_NUMBER_LIST_ITEMS = 4096
DEFAULT_CALLSIGN = "N0CALL"
QST = "QST"


class CQtaError(Exception):
    pass


@dataclass
class CQtaPacket:
    source: str
    destination: str
    number: int
    requests: list[int] = field(default_factory=list)
    no_content: list[int] = field(default_factory=list)
    text: str = ""


def _is_alnum(c: str) -> bool:
    return "0" <= c <= "9" or "A" <= c <= "Z"


def is_valid_callsign(s: str) -> bool:
    if not 1 <= len(s) <= 16 or not s.isascii() or not _is_alnum(s[0]):
        return False
    return all(_is_alnum(c) or c in "/-" for c in s[1:])


def _ascii_digits(s: str) -> bool:
    return bool(s) and all("0" <= c <= "9" for c in s)


def parse_number_list(value: str) -> list[int]:
    if not value:
        raise CQtaError("Packet number lists must not be empty.")
    numbers: list[int] = []
    previous_end = -1
    for item in value.split(","):
        parts = item.split("-")
        if len(parts) > 2 or not all(_ascii_digits(p) for p in parts):
            raise CQtaError(f"Invalid packet number or range: '{item}'.")
        first, last = int(parts[0]), int(parts[-1])
        if first > last:
            raise CQtaError(f"Descending packet range: '{item}'.")
        if first <= previous_end:
            raise CQtaError("Packet numbers must be ascending and non-overlapping.")
        if len(numbers) + (last - first + 1) > MAX_NUMBER_LIST_ITEMS:
            raise CQtaError(f"A number list cannot expand to more than {MAX_NUMBER_LIST_ITEMS} packet IDs.")
        numbers.extend(range(first, last + 1))
        previous_end = last
    return numbers


def format_number_list(numbers) -> str:
    ordered = sorted(set(numbers))
    if not ordered:
        raise CQtaError("Packet number lists must not be empty.")
    if ordered[0] < 0:
        raise CQtaError("Packet numbers must be non-negative.")
    ranges: list[str] = []
    start = end = ordered[0]

    def flush() -> None:
        ranges.append(str(start) if start == end else f"{start}-{end}")

    for n in ordered[1:]:
        if n == end + 1:
            end = n
            continue
        flush()
        start = end = n
    flush()
    return ",".join(ranges)


def encode(p: CQtaPacket) -> bytes:
    source, destination = p.source.upper(), p.destination.upper()
    if not is_valid_callsign(source) or source == QST:
        raise CQtaError(f"Invalid source callsign: '{p.source}'.")
    if not is_valid_callsign(destination):
        raise CQtaError(f"Invalid destination callsign: '{p.destination}'.")
    if p.number < 0:
        raise CQtaError("Packet numbers must be non-negative.")
    if destination == QST and (p.number != 0 or p.requests or p.no_content):
        raise CQtaError("QST calls must use packet 0 and cannot carry number lists.")
    suffix = ""
    if p.requests:
        suffix += "{" + format_number_list(p.requests) + "}"
    if p.no_content:
        suffix += "[" + format_number_list(p.no_content) + "]"
    return f"{source}>{destination} {p.number}{suffix}:{p.text}".encode("utf-8")


def decode(data: bytes) -> CQtaPacket:
    try:
        decoded = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        raise CQtaError("Packet must be valid UTF-8.") from None
    colon = decoded.find(":")
    if colon < 0:
        raise CQtaError("Packet is missing the header/text separator.")
    header, text = decoded[:colon], decoded[colon + 1:]
    if not header.isascii():
        raise CQtaError("Packet header must be ASCII.")
    bad = CQtaError("Packet header has an invalid format.")
    gt = header.find(">")
    if gt < 0:
        raise bad
    source = header[:gt]
    rest = header[gt + 1:]
    sp = rest.find(" ")
    if sp < 0:
        raise bad
    destination = rest[:sp]
    tail = rest[sp + 1:]
    n = 0
    while n < len(tail) and "0" <= tail[n] <= "9":
        n += 1
    digits = tail[:n]
    if not digits or not is_valid_callsign(source) or not is_valid_callsign(destination):
        raise bad
    number = int(digits)
    tail = tail[n:]

    requests: list[int] = []
    no_content: list[int] = []
    seen_requests = seen_no_content = False
    while tail:
        if tail[0] == "{" and not seen_requests:
            end = tail.find("}")
            if end < 0:
                raise CQtaError("Unclosed request list.")
            requests = parse_number_list(tail[1:end])
            seen_requests = True
            tail = tail[end + 1:]
        elif tail[0] == "[" and not seen_no_content:
            end = tail.find("]")
            if end < 0:
                raise CQtaError("Unclosed no-content list.")
            no_content = parse_number_list(tail[1:end])
            seen_no_content = True
            tail = tail[end + 1:]
        else:
            raise CQtaError("Invalid or repeated packet number list.")
    if source == QST:
        raise CQtaError("QST cannot be a packet source.")
    if destination == QST and (number != 0 or requests or no_content):
        raise CQtaError("QST calls must use packet 0 and cannot carry number lists.")
    return CQtaPacket(source, destination, number, requests, no_content, text)
