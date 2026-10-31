# CQta protocol

This is the canonical protocol specification, maintained alongside the CQta
Python reference implementation. This document describes the initial draft. It
is not yet a versioned or finalized standard.

CQta carries text messages over UDP on a network interface that is assumed to
be a tunnel over a radio link. Every CQta datagram is sent to that interface's
broadcast address on UDP port **37373**. The address and port are transport
details; the CQta header identifies the intended recipient.

## Wire format

Each UDP payload has this form:

```text
SOURCE>DESTINATION NUMBER{REQUESTS}[NO-CONTENT]:TEXT
```

The optional `{REQUESTS}` and `[NO-CONTENT]` sections may appear in either
order. Omit a section when it has no entries. For example:

```text
W1ABC>N0XYZ 12{3-7,9}[5-6]:The requested text is being resent.
```

The header is ASCII. `TEXT` is UTF-8; the first colon separates the header
from the text, and any further colons belong to the text. An empty `TEXT` is
valid. A datagram is one complete CQta packet: the format does not define
fragmentation or continuation across datagrams.

### Header fields

- **SOURCE** is the transmitting station's callsign.
- **DESTINATION** is the intended station's callsign, or the special value
  `QST` for a message to all stations.
- **NUMBER** is the decimal number of this packet in the source's outgoing
  stream to that destination. It is a non-negative integer. A `QST` call and
  the opening packet of a directed QSO use number `0`. Repeated `QST` calls
  use `0`. The first packet numbered `0` received from a peer establishes
  that peer's direction in the QSO; once packets have already been received
  from that peer, a subsequent packet numbered `0` signals that the peer
  wants to restart the QSO. On restart, both stations should discard their
  sequence and retransmission state for that directed QSO. A station
  initiating a restart resets its local state before sending its new packet
  `0`. After the opening packet, each new packet in that direction increments
  the number by one. Do not wrap the number during a QSO.
- **REQUESTS**, in braces, lists packet numbers from the destination's
  outgoing stream that the source has not received and is asking to have
  retransmitted.
- **NO-CONTENT**, in brackets, lists packet numbers from the source's own
  outgoing stream that had no text content (keepalives). It tells the
  destination that those packets have no text to retransmit.
- **TEXT** is the message content. It may be empty, including for keepalives.

For a directed QSO, a sequence belongs to the ordered `SOURCE>DESTINATION`
pair. For example, `W1ABC>N0XYZ` and `W1ABC>K2DEF` have separate sequences,
and `W1ABC>N0XYZ` and `N0XYZ>W1ABC` also have separate sequences. This allows
a station to conduct QSOs with multiple stations in parallel, with each
direction numbered independently. `QST` calls are not part of a directed
QSO's sequence: each call uses number `0`, even when the station makes
repeated calls.

An empty `SOURCE>QST 0:` packet is a **beacon**. It is not a user-visible
broadcast message and does not belong to a sequence; receivers may use it to
update the sender's last-heard time. Beacons may be repeated with packet
number `0`, like other QST calls, and must not be shown as transcript messages.

### Number lists

Each list contains comma-separated decimal numbers and inclusive ranges.
For example, `3-7,9` means `3, 4, 5, 6, 7, 9`. Numbers and ranges in a list
must be in ascending order, with no duplicates or overlap. Whitespace is not
allowed inside the header. Empty lists and descending ranges are invalid.

Requests refer to the destination's sequence in this directed QSO;
no-content confirmations refer to the source's sequence in this QSO. The same
number may appear in both lists because it identifies different packets in
each direction. `QST` calls are not part of a directed QSO's sequence and
must not include either list.

## Examples

### CQ call and start a directed QSO

```text
W1ABC>QST 0:CQ
W1ABC>N0XYZ 0:Hello, N0XYZ. This is W1ABC.
N0XYZ>W1ABC 0:Hello, W1ABC. I hear you.
```

Every CQ call uses number `0`, including repeated calls. When N0XYZ answers
and the directed QSO begins, each station starts its own direction at `0`.
Subsequent new packets in each direction are numbered `1`, `2`, and so on.
The first received `0` in each direction establishes that direction; a later
received `0` from that peer restarts the QSO and clears both directions'
tracked sequence state. Because packet `0` is also the restart signal, a
delayed or retransmitted opening packet cannot be distinguished from an
intentional restart. Implementations should avoid retransmitting an opening
packet after the QSO is established.

Empty broadcast messages can be used as beacons to signal presence.

### Restart an active QSO

If W1ABC sends packet `0` to N0XYZ after N0XYZ has already received packets
from W1ABC in their current QSO, that packet signals a restart. N0XYZ clears
the tracked outgoing packets, missing packet numbers, sequence counters, and
empty-packet confirmations for the `W1ABC>N0XYZ` and `N0XYZ>W1ABC` directions,
then processes the received packet `0` as the opening packet of the new QSO.
The first `0` received from W1ABC when a QSO is initially established is not
a restart.

This restart signal is intentionally lightweight, but it means delayed or
retransmitted packet `0` can restart an active QSO. The protocol has no session
identifier with which to distinguish those cases.

### Request missing packets

If W1ABC has received N0XYZ packets `0`, `1`, `2`, `8`, and `10`, it can ask
N0XYZ to resend the gaps it has observed:

```text
W1ABC>N0XYZ 4{3-7,9}:Please resend those packets.
```

The request list can accompany any new packet from W1ABC, including a
keepalive. A receiver may wait and piggyback a request on its next outgoing
packet; it does not have to send a request immediately when it notices a gap.
It may also send a request sooner, for example in an empty keepalive. The
receiver should continue including unresolved missing numbers in later
packets until they are received or confirmed empty. A sender may retransmit
them but is not required to. A receiver must not infer that packets after its
highest received number are missing.

To retransmit a packet, the sender sends its original packet number and
original text again; it does not allocate a new number. The retransmission
may include current request or no-content lists. Receivers should treat a
repeated nonzero number within the same directed QSO as a duplicate rather
than displaying the same text twice. Because a received `0` after QSO
establishment means restart, implementations should not retransmit packet
`0` once the QSO is underway. If a station is asked to retransmit a packet
that it no longer remembers (or must not retransmit, such as packet `0`), it
answers as if that packet were empty: it lists the number in the NO-CONTENT
brackets of its next packets for as long as the number appears in the
requests, so the requester stops asking for it. Numbers the station has not
yet sent are ignored. This duplicate rule does not apply to `QST`
calls: each `SOURCE>QST 0` is a new call, even if its text matches an earlier
call.

### Close a QSO with QRT

Applications may treat a directed message whose text is exactly `QRT` as the
remote station closing the QSO. The comparison is exact: `QRT `, ` QRT`, and
`QRT.` are ordinary message text. A receiving application may close the
conversation UI and stop keepalives without sending a separate close packet.
Closing a QSO locally should send a numbered `QRT` message, then discard the
local sequence and retransmission state for that peer.

If the remote station misses that `QRT` and later sends another packet, the
local application may treat it as a new QSO. It starts tracking the remote
direction at the first packet number it observes and does not request earlier
numbers from before this QSO was reopened. Gaps after that first observed
packet are still tracked normally. The two directions are independent, so the
local station's new outgoing sequence starts at `0`.

### Confirm empty keepalives

If N0XYZ's packets `3` through `6` and `8` were empty keepalives, it can tell
W1ABC those packets have no text to recover:

```text
N0XYZ>W1ABC 11[3-6,8]:
```

An empty packet still consumes a number in its sender's stream. A receiver
that gets a no-content confirmation should stop requesting those numbers.
Keepalives do not have a separate wire representation; they are ordinary
numbered packets with empty text.

### Mix requests and confirmations

```text
N0XYZ>W1ABC 11{2,5-6}[3-4]:
```

This requests W1ABC packets `2`, `5`, and `6`, and confirms that N0XYZ
packets `3` and `4` were empty. The two lists refer to opposite streams.

### Broadcast

```text
W1ABC>QST 0:CQ
```

`QST` means all stations. Each CQ call uses number `0` and is not included in
a directed QSO's packet sequence. Text beginning with `CQ` is otherwise
ordinary content. The CQta GUI highlights a received `CQ` message and lets
the user open a QSO with its sender by double-clicking it.

## Keepalive timing

When a directed QSO is idle, a station should send an empty keepalive
approximately every 25 to 35 seconds, choosing the interval randomly in that
range. A keepalive increments that direction's QSO sequence just like a
text-bearing packet. Keepalives help expose gaps in the sequence, but do not
guarantee delivery or prove that a peer is reachable. This timing rule does
not apply to `QST` calls.

## Datagram size

Implementations should avoid IP fragmentation. The maximum CQta payload
depends on the path MTU and IP version: subtract the IP and UDP headers from
the MTU to obtain the maximum UDP payload, and keep the CQta header and text
within that budget. For example, with a 256-byte IP MTU, the theoretical UDP
payload limit is 228 bytes for IPv4 with a 20-byte IP header, or 208 bytes for
IPv6 with a 40-byte IP header. Tunnels and IP options can reduce those limits;
use a smaller configured limit when necessary.

This draft does not define message splitting, truncation, or a mechanism to
negotiate a payload limit. Applications should reject or clearly report
messages that exceed their configured datagram budget rather than silently
truncating them.

The current CQta GUI implementation limits each decoded number list and the
number of inferred missing packet IDs to 4096. This is an application resource
limit, not a limit on the wire format; a later implementation could track
large ranges without expanding every ID in memory.

## Limitations and possible improvements

- **Conversation identity:** Numbering is scoped to the ordered
  `SOURCE>DESTINATION` pair, so separate partners can have concurrent QSOs.
  Packet `0` can restart a QSO with the same partner, but the protocol cannot
  distinguish that intent from a delayed or retransmitted opening packet. A
  future revision could add a compact session identifier (or sender epoch) to
  disambiguate restarts and stale packets.
- **Retransmission policy:** Requests are advisory and have no expiry,
  acknowledgement, or retry limit. A receiver can piggyback requests on its
  next ordinary message or keepalive, or send them sooner; implementations
  need local policy for how long to retain packets and how frequently to
  repeat requests.
- **Unbounded gap lists:** A long outage can make a missing-packet list large.
  A future revision could cap each list or define a compact cumulative
  acknowledgement scheme.
- **No security or delivery guarantee:** This format provides no
  authentication, encryption, or guaranteed delivery. Those concerns depend
  on the underlying link and are not solved by the message syntax.