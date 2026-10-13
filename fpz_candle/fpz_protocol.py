"""Evidence-derived FPZ framing, with no calibration or GRBL state inference.

DesignPro's relative/absolute XY formatters emit M/MA with integer counts;
the integer speed branch emits S, and the spindle-off wrapper emits R0.
Its position reader accepts R/X/Z prefixes and CR or NUL termination. Status
F is accepted by the application, but neither its receipt nor a successful
write proves mechanical completion. No device access occurs on import.

Integration summary:
* Encoders: RP CR, C CR, M dx,dy CR, MA x,y CR, R0 CR, S integer CR.
* Decoder: position (raw signed counts), xon/xoff, error, unknown, and F
  tagged with explicit PendingContext or classified as unsolicited_f.
* request_position: exactly one RP on an already-open exclusive transport;
  1000ms overall default, 10ms incomplete-poll pacing, no resend or reset.
* No mm conversion, coordinate origins, arbitrary Z, Idle state, firmware
  version, or mechanical-completion receipt is inferred by this module.
"""

from dataclasses import dataclass
from enum import Enum
import math
import re
import time
from typing import Callable

from .model import Transport, TransportError


class ProtocolError(TransportError):
    """An ambiguous or malformed response; do not continue the transaction."""


class ResponseDeadline(ProtocolError):
    """No complete position response within the overall deadline."""


class PendingContext(str, Enum):
    POSITION = "position"
    BARRIER = "barrier"
    COMMAND = "command"


ERROR_CODES = {
    "A": -24, "C": -21, "D": -40, "E": -25, "H": -41,
    "I": -22, "L": -23, "M": -20, "P": -8, "S": -26, "T": -42,
}


@dataclass(frozen=True)
class Response:
    kind: str
    raw: bytes
    context: PendingContext | None = None
    counts: tuple[int, int, int] | None = None
    application_error: int | None = None


def _integer(value: int) -> str:
    if type(value) is not int or not -(2**31) <= value < 2**31:
        raise ValueError("Signed 32-bit controller integer required")
    return str(value)


def position_request() -> bytes:
    return b"RP\r"


def completion_barrier() -> bytes:
    """Original C format; its mechanical effect/completion remains unverified."""
    return b"C\r"


def relative_xy_counts(dx: int, dy: int) -> bytes:
    return f"M {_integer(dx)},{_integer(dy)}\r".encode("ascii")


def absolute_xy_counts(x: int, y: int) -> bytes:
    return f"MA {_integer(x)},{_integer(y)}\r".encode("ascii")


def spindle_off() -> bytes:
    """Queued spindle-off command, not an emergency stop."""
    return b"R0\r"


def feed_controller_units(value: int) -> bytes:
    """Integer S branch only; caller must establish units, limits and mode.

    No float truncation, mm/min conversion, or machine-specific clamp is hidden
    here. This emits the format only, not authorization to use a particular feed.
    """
    return f"S {_integer(value)}\r".encode("ascii")


class StreamDecoder:
    """Packet boundaries have no meaning; callers own pending-request context.

    XON/XOFF are emitted separately, including between position payload bytes.
    A malformed/oversized payload latches failure rather than guessing a new
    frame boundary. Feed returns no events on an empty (ZLP) packet. Context
    changes while a payload is incomplete are rejected. F never yields a
    completed/Idle status, and does not automatically consume caller context.
    """

    def __init__(self, *, max_payload_bytes: int = 128):
        if type(max_payload_bytes) is not int or not 1 <= max_payload_bytes <= 4096:
            raise ValueError("Payload bound must be an integer between 1 and 4096")
        self.max_payload_bytes = max_payload_bytes
        self._pending = bytearray()
        self._context = None
        self.faulted = False

    @property
    def incomplete(self) -> bool:
        return bool(self._pending)

    def _fail(self, message: str):
        self.faulted = True
        raise ProtocolError(message)

    def feed(self, packet: bytes, *, context: PendingContext | None = None) -> list[Response]:
        if not isinstance(packet, bytes):
            raise TypeError("Response packet must be bytes")
        if context is not None and not isinstance(context, PendingContext):
            raise TypeError("Use an explicit PendingContext")
        if self.faulted:
            raise ProtocolError("Decoder fault is latched; create a new session")
        if self.incomplete and context != self._context:
            self._fail("Pending context changed inside a position payload")
        events = []
        for value in packet:
            raw = bytes((value,))
            if value in (0x11, 0x13):
                events.append(Response("xon" if value == 0x11 else "xoff", raw, context))
                continue
            if self.incomplete:
                if value in (0, 13):
                    frame = bytes(self._pending) + raw
                    payload = bytes(self._pending[1:])
                    if not re.fullmatch(rb" *[+-]?[0-9]+[ ,]+[+-]?[0-9]+[ ,]+[+-]?[0-9]+ *", payload):
                        self._fail("Position payload must contain exactly three signed integers")
                    counts = tuple(int(x) for x in re.split(rb"[ ,]+", payload.strip()))
                    if any(not -(2**31) <= x < 2**31 for x in counts):
                        self._fail("Position count is outside signed 32-bit range")
                    events.append(Response("position", frame, self._context, counts))
                    self._pending.clear()
                    self._context = None
                else:
                    if value not in b"0123456789+-, ":
                        self._fail("Unexpected byte inside position payload")
                    if len(self._pending) - 1 >= self.max_payload_bytes:
                        self._fail("Position payload exceeds configured bound")
                    self._pending.append(value)
                continue
            if value in b"RXZ":
                self._pending.append(value)
                self._context = context
            elif value == ord("F"):
                events.append(Response("f_response" if context is not None else "unsolicited_f", raw, context))
            elif chr(value) in ERROR_CODES:
                events.append(Response("error", raw, context, application_error=ERROR_CODES[chr(value)]))
            else:
                events.append(Response("unknown", raw, context))
        return events


@dataclass(frozen=True)
class PositionResult:
    counts: tuple[int, int, int]
    responses: tuple[Response, ...]


def request_position(
    transport: Transport, *, deadline_ms: int = 1000,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PositionResult:
    """One RP write, bounded polling, no open/close/init/retry or state inference.

    Caller owns an already-open exclusive transport with no pending/stale
    responses. An adapter must expose a writable positive integer timeout_ms
    (as USBTransport does) and honor it per operation. The base Transport
    protocol alone cannot guarantee a finite wait, so other adapters are
    rejected before sending. Original timeout is restored on every exit.
    A partial/late/failed response leaves session recovery to the caller; never
    retry RP automatically. ZLPs are paced at 10ms without marking a fault.
    """
    if type(deadline_ms) is not int or deadline_ms <= 0:
        raise ValueError("Positive integer overall deadline_ms required")
    original_timeout = getattr(transport, "timeout_ms", None)
    if type(original_timeout) is not int or original_timeout <= 0:
        raise ValueError("Transport needs a writable positive integer timeout_ms")
    end = clock() + deadline_ms / 1000
    decoder = StreamDecoder()
    responses = []

    def remaining_ms():
        remaining = math.floor((end - clock()) * 1000)
        if remaining < 1:
            raise ResponseDeadline("RP overall deadline expired without a complete response")
        return min(original_timeout, remaining)

    try:
        transport.timeout_ms = remaining_ms()
        transport.write_command(position_request())
        while True:
            transport.timeout_ms = remaining_ms()
            packet = transport.read_packet()
            if clock() >= end:
                raise ResponseDeadline("RP response arrived after its overall deadline")
            events = decoder.feed(packet, context=PendingContext.POSITION)
            responses.extend(events)
            if any(e.kind in ("error", "unknown") for e in events):
                raise ProtocolError("RP received an error or unknown response; no position accepted")
            positions = [e for e in events if e.kind == "position"]
            if positions:
                if len(positions) != 1 or decoder.incomplete:
                    raise ProtocolError("Multiple or trailing partial position responses are ambiguous")
                return PositionResult(positions[0].counts, tuple(responses))
            # Pace all incomplete polls, including repeated standalone F/XON.
            delay = min(0.010, max(0.0, end - clock()))
            sleep(delay)
    finally:
        transport.timeout_ms = original_timeout
