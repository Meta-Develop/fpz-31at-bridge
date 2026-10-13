"""Backends keep synthetic state separate from hardware-derived state."""

from .model import (BridgeError, Command, FPZCodec, GCodeError, MachineStatus, Receipt,
                    Transport, UnknownFPZCodec, Unsupported)


class UnavailableTransport:
    def open(self):
        raise BridgeError("No hardware transport configured; USB is disabled")

    def close(self):
        pass

    def write_command(self, frame):
        raise BridgeError("USB is disabled")

    def read_packet(self):
        raise BridgeError("USB is disabled")


class HardwareBackend:
    simulation = False
    description = "FPZ HARDWARE UNAVAILABLE: protocol and stop/status mappings are unverified"

    def __init__(self, transport: Transport | None = None, codec: FPZCodec | None = None):
        self.transport = transport if transport is not None else UnavailableTransport()
        self.codec = codec if codec is not None else UnknownFPZCodec()
        self.opened = False
        self.ever_opened = False
        self.fault: str | None = None

    def _fault(self, error: Exception):
        self.fault = str(error)

    def _check(self):
        if self.fault:
            raise BridgeError(f"Hardware fault is latched: {self.fault}")

    @staticmethod
    def _status(status: MachineStatus):
        if not isinstance(status, MachineStatus) or not status.measured:
            raise BridgeError("Codec did not provide measured hardware status")
        return status

    def _receipt(self, receipt: Receipt):
        if not isinstance(receipt, Receipt) or not receipt.confirmed or not receipt.completed:
            raise BridgeError("Hardware acceptance/completion is unproven")
        if receipt.status is not None:
            self._status(receipt.status)
        if receipt.progress:
            raise BridgeError("Synthetic progress events cannot be used in hardware mode")
        return receipt

    def execute(self, command: Command) -> Receipt:
        self._check()
        # This must reject unknown commands BEFORE opening the USB device.
        self.codec.validate(command)
        try:
            if not self.opened:
                self.ever_opened = True  # Even failed open must not be hidden by reset.
                self.transport.open()
                self.opened = True
            receipt = self._receipt(self.codec.execute(command, self.transport))
            if command.program_end:
                status = receipt.status
                if (status is None or status.state != "Idle" or status.spindle_rpm != 0
                        or status.spindle_enabled is not False):
                    raise BridgeError("Program end requires measured Idle and confirmed spindle off")
            return receipt
        except Exception as exc:
            self._fault(exc)
            raise BridgeError(f"Hardware command failed: {exc}") from exc

    def status(self) -> MachineStatus:
        if self.fault or not self.opened:
            return MachineStatus("Alarm")  # No invented coordinate/idle telemetry.
        try:
            return self._status(self.codec.status(self.transport))
        except Exception as exc:
            self._fault(exc)
            raise BridgeError(f"Hardware status failed: {exc}") from exc

    def realtime(self, action: str):
        if action == "reset" and not self.ever_opened:
            self.fault = None
            return
        # Stop semantics are deliberately unavailable in the foundation, even
        # when a codec is injected. ESC does not prove GRBL feed-hold/reset.
        self.fault = f"{action} is unavailable; actual motion state is unknown"
        raise Unsupported(self.fault)

    def close(self):
        self.transport.close()
        self.opened = False


class SimulationBackend:
    """Poll-driven offline model; never imported as a hardware fallback.

    After queued moves, the first status poll reports Run at the starting point,
    the next reports Idle at the planned endpoint. This models no machine timing,
    acceleration, trajectory or clearance and validates only the client protocol.
    """

    simulation = True
    description = "SIMULATION ONLY: synthetic coordinates; no USB or motors"

    def __init__(self):
        self.position = (0.0, 0.0, 0.0)
        self.planned_position = self.position
        self.spindle = 0.0
        self.hold = False
        self.moving = False
        self.reported_run = False

    def execute(self, command: Command) -> Receipt:
        if self.hold:
            raise GCodeError(9, "Simulation is on hold; this block was not accepted")
        position = list(self.planned_position)
        for index, axis in enumerate("XYZ"):
            if axis in command.axes:
                value = command.axes[axis] * command.modes.scale
                position[index] = value + (position[index] if command.modes.distance == "G91" else 0)
        # Validate before mutating state, including unit-conversion overflow.
        spindle = command.modes.desired_rpm if command.modes.spindle == "M3" else 0.0
        status = MachineStatus("Idle", tuple(position), 0.0, spindle)
        if command.axes:
            self.planned_position = status.position_mm
            self.moving = True
            self.reported_run = False
        self.spindle = spindle
        if command.program_end:
            # Program flow occurs after all block motion. Record the actual
            # synthetic pre-drain state, then synchronize and turn the model's
            # spindle off. Session emits these ordered events around the ACK;
            # hardware mode is expressly prohibited from using this mechanism.
            progress = ()
            if self.moving:
                progress = (MachineStatus("Run", self.position, 0.0, self.spindle,
                                          work_offset_mm=(0.0, 0.0, 0.0)),)
            self.position = self.planned_position
            self.moving = False
            self.reported_run = False
            self.spindle = 0.0
            status = MachineStatus("Idle", self.position, 0.0, 0.0,
                                   work_offset_mm=(0.0, 0.0, 0.0), spindle_enabled=False)
            return Receipt(True, True, status, progress)
        return Receipt(True, not self.moving)

    def status(self) -> MachineStatus:
        if self.hold:
            state = "Hold:0"
        elif self.moving and not self.reported_run:
            self.reported_run = True
            state = "Run"
        else:
            self.position = self.planned_position
            self.moving = False
            state = "Idle"
        return MachineStatus(state, self.position, 0.0, self.spindle,
                             work_offset_mm=(0.0, 0.0, 0.0))

    def realtime(self, action: str):
        if action == "hold":
            self.hold = True
        elif action == "resume":
            self.hold = False
        elif action in {"reset", "jog-cancel"}:
            self.hold = False
            self.moving = False
            self.reported_run = False
            self.planned_position = self.position
            if action == "reset":
                self.spindle = 0.0
        else:
            raise Unsupported(f"Simulation does not implement {action}")

    def close(self):
        pass
