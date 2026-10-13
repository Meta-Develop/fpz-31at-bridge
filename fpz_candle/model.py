"""Shared contracts. No firmware command values or machine calibration live here."""

from dataclasses import dataclass, replace
from math import isfinite
from typing import Protocol


class BridgeError(Exception):
    """An operation failed without an acknowledgement to the serial client."""


class Unsupported(BridgeError):
    pass


class TransportError(BridgeError):
    pass


class GCodeError(BridgeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ModalState:
    motion: str = "G0"
    units: str = "G21"
    distance: str = "G90"
    feed_mm_min: float = 0.0
    desired_rpm: float = 0.0
    spindle: str = "M5"

    @property
    def scale(self) -> float:
        return 25.4 if self.units == "G20" else 1.0

    def report(self) -> str:
        return (f"[GC:{self.motion} G54 G17 {self.units} {self.distance} G94 "
                f"{self.spindle} M9 T0 F{self.feed_mm_min / self.scale:g} "
                f"S{self.desired_rpm:g}]")

    def after_program_end(self) -> "ModalState":
        # GRBL v1.1h.20190825 gcode.c program flow: reset G1/G17/G90/G94/
        # G54/M5/M9; units, F and S are retained. Other modes here are fixed.
        return replace(self, motion="G1", distance="G90", spindle="M5")


@dataclass(frozen=True)
class Command:
    """Validated GRBL request, with prospective modes; never a firmware string.

    Axes remain in the requested units/distance mode. A real codec must resolve
    these against measured position, established origins and proven calibration.
    G2/G3 are intentionally unavailable until an arc implementation is supplied.
    """

    kind: str  # gcode or jog
    source: str
    words: tuple[tuple[str, float], ...]
    modes: ModalState
    program_end: str | None = None  # M2 or M30; performed after this block's motion.

    @property
    def axes(self) -> dict[str, float]:
        return {key: value for key, value in self.words if key in "XYZ"}

    @property
    def needs_hardware(self) -> bool:
        return (bool(self.axes) or any(key == "M" for key, _ in self.words)
                or (self.modes.spindle == "M3" and any(key == "S" for key, _ in self.words)))


@dataclass(frozen=True)
class MachineStatus:
    state: str
    position_mm: tuple[float, float, float] | None = None
    feed_mm_min: float | None = None
    spindle_rpm: float | None = None
    measured: bool = False
    work_offset_mm: tuple[float, float, float] | None = None
    spindle_enabled: bool | None = None

    def __post_init__(self):
        if self.state not in {"Idle", "Run", "Hold:0", "Hold:1", "Jog", "Alarm", "Door:0"}:
            raise ValueError("Unrecognized machine state")
        for vector in (self.position_mm, self.work_offset_mm):
            if vector is not None and (len(vector) != 3 or not all(isfinite(x) for x in vector)):
                raise ValueError("Position must contain three finite coordinates")
        for value in (self.feed_mm_min, self.spindle_rpm):
            if value is not None and (not isfinite(value) or value < 0):
                raise ValueError("Telemetry must be finite and nonnegative")


@dataclass(frozen=True)
class Receipt:
    """Real codecs must establish both fields from hardware responses.

    A successful USB write alone proves neither acceptance nor completion.
    Requiring completion bounds this initial bridge to one hardware operation.
    """

    confirmed: bool
    completed: bool
    status: MachineStatus | None = None
    progress: tuple[MachineStatus, ...] = ()  # Ordered synthetic events; never real fallback.


class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def write_command(self, frame: bytes) -> None: ...
    def read_packet(self) -> bytes: ...


class FPZCodec(Protocol):
    """Injection point for the separately researched FPZ protocol.

    validate MUST be pure and reject unsupported semantics before USB opens.
    Operations run serially in the runtime's backend-owner process. They must
    bound waits and must not report stop/hold unless the controller provides it.
    The serial loop can inhibit dispatch and discard late results while this
    process waits, but cannot interrupt a physical operation. No production
    motion codec or verified physical stop/status path is provided yet.
    """

    def validate(self, command: Command) -> None: ...
    def execute(self, command: Command, transport: Transport) -> Receipt: ...
    def status(self, transport: Transport) -> MachineStatus: ...
    def realtime(self, action: str, transport: Transport) -> Receipt: ...


class UnknownFPZCodec:
    def validate(self, command: Command) -> None:
        raise Unsupported("FPZ command encoding is not verified")

    def execute(self, command: Command, transport: Transport) -> Receipt:
        raise Unsupported("FPZ command encoding is not verified")

    def status(self, transport: Transport) -> MachineStatus:
        raise Unsupported("FPZ measured status decoding is not verified")

    def realtime(self, action: str, transport: Transport) -> Receipt:
        raise Unsupported(f"FPZ {action} semantics are not verified")
