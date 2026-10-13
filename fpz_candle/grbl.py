"""GRBL 1.1 line interface, including Candle's reset/S/$G/status handshake."""

from .gcode import parse_block
from .model import BridgeError, Command, GCodeError, MachineStatus, ModalState, Receipt, Unsupported

# GRBL's public RX capacity; clients may stream this many bytes before replies.
RX_BYTES = 128
BANNER = "Grbl 1.1h ['$' for help]"
REALTIME = {0x18: "reset", ord("?"): "status", ord("!"): "hold",
            ord("~"): "resume", 0x85: "jog-cancel"}


def message(text: str) -> str:
    return "[MSG:" + text.replace("\r", " ").replace("\n", " ").replace("]", ")") + "]"


def format_status(status: MachineStatus, *, fault: bool = False) -> str:
    fields = ["Alarm" if fault else status.state]
    if status.position_mm is not None:
        fields.append("MPos:" + ",".join(f"{x:.3f}" for x in status.position_mm))
    if status.work_offset_mm is not None:
        fields.append("WCO:" + ",".join(f"{x:.3f}" for x in status.work_offset_mm))
    if status.feed_mm_min is not None and status.spindle_rpm is not None:
        fields.append(f"FS:{status.feed_mm_min:g},{status.spindle_rpm:g}")
    return "<" + "|".join(fields) + ">"


class GRBLSession:
    def __init__(self, backend):
        self.backend = backend
        self.modes = ModalState()
        self.fault: str | None = None
        self._pending = bytearray()
        self._bad_line: int | None = None
        self._after_cr = False

    def banner(self) -> list[str]:
        return [BANNER, message(self.backend.description)]

    def _error(self, exc: Exception) -> list[str]:
        if isinstance(exc, GCodeError):
            return [message(str(exc)), f"error:{exc.code}"]
        if isinstance(exc, Unsupported):
            return [message(str(exc)), "error:20"]
        self.fault = str(exc)
        return [message(f"Operation failed; subsequent commands are blocked: {exc}"), "error:9"]

    def prepare_line(self, text: str) -> Command | list[str]:
        """Pure parsing/local queries. The dispatcher owns all session mutation."""
        line = text.strip().upper()
        if line == "$G":
            return [self.modes.report(), "ok"]
        if line == "$I":
            mode = "SIMULATION" if self.backend.simulation else "HARDWARE-UNAVAILABLE"
            return [f"[VER:1.1h:FPZ-BRIDGE-{mode}]", "ok"]
        if line == "$":
            return ["[HLP:$G $I $J= ? ! ~ ctrl-x]", message(self.backend.description), "ok"]
        if self.fault:
            raise GCodeError(9, "Bridge fault is latched")
        if line.startswith("$") and not line.startswith("$J="):
            raise Unsupported("Settings, unlocking, homing, offsets and probing are unavailable")
        return parse_block(line[3:] if line.startswith("$J=") else line,
                           self.modes, jog=line.startswith("$J="))

    def finish_command(self, command: Command, receipt: Receipt | None = None) -> list[str]:
        """Commit a matching successful result. Never called for stale work."""
        if command.program_end:
            if receipt is None or not receipt.confirmed or not receipt.completed:
                raise BridgeError("Program end did not complete")
            self.modes = command.modes.after_program_end()
            output = [format_status(status) for status in receipt.progress]
            output += ["[MSG:Pgm End]", "ok"]
            if receipt.status is not None:
                output.append(format_status(receipt.status))
            return output
        # S while M5 only records desired RPM. M3 requires a backend receipt.
        if command.kind != "jog":
            self.modes = command.modes
        return ["ok"]

    def line(self, text: str) -> list[str]:
        """Synchronous convenience API; the PTY uses ResponsiveDispatcher."""
        try:
            command = self.prepare_line(text)
            if isinstance(command, list):
                return command
            receipt = self.backend.execute(command) if command.needs_hardware else None
            return self.finish_command(command, receipt)
        except Exception as exc:
            return self._error(exc)

    def realtime(self, action: str) -> list[str]:
        if action == "status":
            try:
                status = self.backend.status()
                # HardwareBackend enforces provenance; no estimate fills missing data.
                return [format_status(status, fault=bool(self.fault))]
            except Exception as exc:
                self.fault = str(exc)
                return [message(str(exc)), "<Alarm>"]
        try:
            if action == "resume" and self.fault:
                return [message("Resume rejected: bridge fault remains latched"), "<Alarm>"]
            if action == "reset":
                self._pending.clear()
                self._bad_line = None
                self._after_cr = False
            self.backend.realtime(action)
            if action == "reset":
                self.modes = ModalState()
                self.fault = None
                return self.banner()
            return []
        except Exception as exc:
            self.fault = str(exc)
            # Realtime messages MUST NOT consume a client's line-response credit.
            return [message(str(exc)), "<Alarm>"]

    def feed(self, data: bytes) -> bytes:
        output = []
        for value in data:
            if value in REALTIME:
                output.extend(self.realtime(REALTIME[value]))
            elif value >= 0x80:
                output.append(message(f"Realtime byte 0x{value:02x} is unavailable"))
            elif value in (10, 13):
                if value == 10 and self._after_cr:
                    self._after_cr = False
                    continue
                if self._bad_line is not None:
                    output.append(f"error:{self._bad_line}")
                else:
                    output.extend(self.line(self._pending.decode("ascii")))
                self._pending.clear()
                self._bad_line = None
                self._after_cr = value == 13
            else:
                self._after_cr = False
                if value < 0x20 and value != 9:
                    self._bad_line = 3
                elif len(self._pending) >= RX_BYTES - 1:
                    self._bad_line = 11
                elif self._bad_line is None:
                    self._pending.append(value)
        return encode(output)


def encode(lines: list[str]) -> bytes:
    return ("\r\n".join(lines) + ("\r\n" if lines else "")).encode("ascii", errors="replace")
