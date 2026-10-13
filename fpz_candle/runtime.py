"""Responsive serial dispatch with one process owning every backend operation.

The parent owns framing and modal commits. No backend method runs in the
parent. An invalidated command may already have reached hardware; inhibition
is not a physical stop. Late results cannot acknowledge or commit that command.
"""

from collections import deque
from dataclasses import dataclass
import multiprocessing
import math
import os
import pty
import select
import tty

from .grbl import GRBLSession, REALTIME, RX_BYTES, encode, format_status, message
from .model import BridgeError, GCodeError, ModalState, Unsupported


def _owner_main(connection, backend):
    """No other process calls this instance's execute/status/realtime/close."""
    try:
        while True:
            try:
                work = connection.recv()
            except EOFError:
                break
            if work is None:
                break
            token, kind, argument = work
            try:
                result = getattr(backend, kind)(argument) if kind != "status" else backend.status()
                failure = None
            except Exception as exc:
                result = None
                failure = ("gcode", exc.code, str(exc)) if isinstance(exc, GCodeError) else (
                    "unsupported" if isinstance(exc, Unsupported) else "failure", None, str(exc))
            connection.send((token, result, failure))
    finally:
        try:
            backend.close()
        finally:
            connection.close()


@dataclass
class _Line:
    text: str
    cost: int
    error: int | None = None


@dataclass
class _Pending:
    token: int
    generation: int
    kind: str
    argument: object
    cost: int = 0
    credited: bool = False


@dataclass(frozen=True)
class ShutdownResult:
    owner_exited: bool
    forced: bool
    operation_abandoned: bool
    exitcode: int | None

    @property
    def clean(self):
        return self.owner_exited and not self.forced and not self.operation_abandoned and self.exitcode == 0


class ResponsiveDispatcher:
    """At most one backend request; at most 127 unacknowledged normal bytes.

    feed()/poll()/close() are called by one serial-loop owner, never concurrently.
    Backend objects must be spawn-picklable and unopened when passed here.
    Only the child process uses their mutable state or opens their transport.
    """

    def __init__(self, backend):
        if getattr(backend, "opened", False) or getattr(backend, "ever_opened", False):
            raise ValueError("Backend must be unopened before creating its owner process")
        self.session = GRBLSession(backend)
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe()
        self._owner = context.Process(target=_owner_main, args=(child, backend), daemon=True,
                                      name="fpz-backend-owner")
        try:
            self._owner.start()
        except BaseException:
            self._connection.close()
            child.close()
            raise
        child.close()
        self._queue = deque()
        self._partial = bytearray()
        self._bad_line = None
        self._discard = False
        self._after_cr = False
        self._pending = None
        self._generation = self._sequence = 0
        self._closed = self._lost = False
        self._shutdown = None
        self._want_status = False

    @property
    def pending(self):
        return self._pending is not None

    @property
    def buffered_bytes(self):
        active = self._pending.cost if self._pending and not self._pending.credited else 0
        return active + sum(line.cost for line in self._queue) + len(self._partial)

    def fileno(self):
        return None if self._closed or self._lost else self._connection.fileno()

    def _submit(self, kind, argument=None, cost=0):
        if self._pending is not None:
            raise RuntimeError("Only one backend request is permitted")
        self._sequence += 1
        self._pending = _Pending(self._sequence, self._generation, kind, argument, cost)
        try:
            self._connection.send((self._sequence, kind, argument))
        except (OSError, BrokenPipeError):
            self._pending = None
            self._lost = True
            raise BridgeError("Backend owner is unavailable")

    def _inhibit(self, reason):
        self._generation += 1
        self._want_status = False
        self.session.fault = reason
        output = [message(reason + "; physical state unknown, no stop confirmed"), "<Alarm>"]
        if self._pending and self._pending.cost and not self._pending.credited:
            output.append("error:9")
            self._pending.credited = True
        while self._queue:
            self._queue.popleft()
            output.append("error:9")
        return output

    def _realtime(self, action):
        if action == "status" and self._pending and not self.session.fault:
            if self._pending.kind == "status":
                return []  # Coalesce queries; the pending query will answer.
            if self.session.backend.simulation:
                self._want_status = True  # Query the model after its short operation.
                return []
        if action == "status" and (self._pending or self.session.fault):
            return [message("Status unavailable while pending or inhibited; physical state unknown"),
                    "<Alarm>"]
        if self._pending:
            if action == "reset":
                self._partial.clear()
                self._bad_line = None
                self._discard = self._after_cr = False
            return self._inhibit(f"{action} received during pending work; dispatch inhibited")
        if self.session.fault and action != "reset":
            return [message("Dispatch remains inhibited; physical state unknown"), "<Alarm>"]
        if not self.session.backend.simulation and action not in {"status", "reset"}:
            return self._inhibit(f"Hardware {action} is unavailable; dispatch inhibited")
        output = []
        if action == "reset":
            # Completed lines get their own rejection credit before a new epoch.
            while self._queue:
                self._queue.popleft()
                output.append("error:9")
            self._partial.clear()
            self._bad_line = None
            self._discard = self._after_cr = False
        self._submit("status" if action == "status" else "realtime",
                     None if action == "status" else action)
        return output

    def _pump(self):
        output = []
        while self._queue and self._pending is None and not self._lost and not self._closed:
            line = self._queue.popleft()
            try:
                if line.error is not None:
                    raise GCodeError(line.error, "Malformed or oversized serial line")
                command = self.session.prepare_line(line.text)
                if isinstance(command, list):
                    output.extend(command)
                elif command.needs_hardware:
                    self._submit("execute", command, line.cost)
                else:
                    output.extend(self.session.finish_command(command))
            except Exception as exc:
                output.extend(self.session._error(exc))
                if self._lost:
                    output.extend(self._inhibit("Backend owner is unavailable"))
        return output

    def feed(self, data: bytes) -> bytes:
        if self._closed:
            raise RuntimeError("Dispatcher is closed")
        output = []
        for value in data:
            if value in REALTIME:
                try:
                    output.extend(self._realtime(REALTIME[value]))
                except BridgeError as exc:
                    output.extend(self._inhibit(str(exc)))
                continue
            if value >= 0x80:
                output.append(message(f"Realtime byte 0x{value:02x} is unavailable"))
                continue
            if value == 10 and self._after_cr:
                self._after_cr = False
                continue
            self._after_cr = value == 13
            if not self._discard and self.buffered_bytes >= RX_BYTES - 1:
                output.extend(self._inhibit("Serial receive window exceeded"))
                self._partial.clear()
                self._bad_line = 11
                self._discard = True
            if value in (10, 13):
                cost = 0 if self._discard else len(self._partial) + 1
                line = _Line(self._partial.decode("ascii"), cost, self._bad_line)
                self._partial.clear()
                self._bad_line = None
                self._discard = False
                if self.session.fault:
                    output.append(f"error:{line.error or 9}")
                else:
                    self._queue.append(line)
            elif not self._discard:
                if value < 0x20 and value != 9:
                    self._bad_line = 3
                self._partial.append(value)
        output.extend(self._pump())
        return encode(output)

    def poll(self) -> bytes:
        if self._closed or self._lost:
            return b""
        output = []
        try:
            if not self._connection.poll():
                return b""
            token, result, failure = self._connection.recv()
            pending = self._pending
            if pending is None or token != pending.token:
                raise BridgeError("Unexpected backend result")
            self._pending = None
            if pending.generation != self._generation:
                return encode(self._pump())  # Already rejected; discard even success.
            if failure is not None:
                kind, code, detail = failure
                error = GCodeError(code, detail) if kind == "gcode" else (
                    Unsupported(detail) if kind == "unsupported" else BridgeError(detail))
                if pending.kind == "execute":
                    output.extend(self.session._error(error))
                else:
                    output.extend(self._inhibit(str(error)))
            elif pending.kind == "execute":
                try:
                    output.extend(self.session.finish_command(pending.argument, result))
                except Exception as exc:
                    output.extend(self.session._error(exc))
            elif pending.kind == "status":
                output.append(format_status(result))
            elif pending.argument == "reset":
                self._generation += 1
                self.session.modes = ModalState()
                self.session.fault = None
                output.extend(self.session.banner())
            if self._want_status and not self.session.fault:
                self._want_status = False
                self._submit("status")
            else:
                output.extend(self._pump())
        except (EOFError, OSError, BrokenPipeError) as exc:
            self._lost = True
            output.extend(self._inhibit(f"Backend owner disconnected: {exc}"))
        except Exception as exc:
            output.extend(self._inhibit(f"Backend result rejected: {exc}"))
        return encode(output)

    def close(self, *, grace_seconds: float = 1.0) -> ShutdownResult:
        """Join boundedly; a stuck child is terminated, never concurrently closed.

        Forced/abandoned shutdown is reported as unclean, not as a physical stop.
        The default grace matches the existing 1000 ms USB operation timeout.
        """
        if self._shutdown is not None:
            return self._shutdown
        if not math.isfinite(grace_seconds) or grace_seconds <= 0:
            raise ValueError("Shutdown grace must be positive")
        self._closed = True
        abandoned = self._pending is not None
        self._generation += 1
        self._queue.clear()
        self._partial.clear()
        try:
            if not self._lost:
                self._connection.send(None)
        except (OSError, BrokenPipeError):
            pass
        self._owner.join(grace_seconds)
        forced = self._owner.is_alive()
        if forced:
            self._owner.terminate()
            self._owner.join(grace_seconds)
        if self._owner.is_alive():
            self._owner.kill()
            self._owner.join(grace_seconds)
        self._connection.close()
        self._shutdown = ShutdownResult(not self._owner.is_alive(), forced, abandoned,
                                        self._owner.exitcode)
        return self._shutdown


class PTYServer:
    """One nonblocking PTY event loop; output backpressure fails closed."""

    OUTPUT_LIMIT = 16 * 1024  # Local serial-output buffer cap; not firmware capacity.

    def __init__(self, backend):
        self.dispatcher = ResponsiveDispatcher(backend)
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        os.fchmod(self.slave, 0o600)
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self.slave)
        self._output = bytearray(encode(self.dispatcher.session.banner()))
        self._closed = False

    def _append(self, data):
        if len(self._output) + len(data) > self.OUTPUT_LIMIT:
            self.dispatcher._inhibit("Serial output backpressure exceeded buffer bound")
            raise BridgeError("Serial output backpressure; physical state unknown")
        self._output.extend(data)

    def step(self, timeout=0.25):
        read_fds = [self.master]
        owner_fd = self.dispatcher.fileno()
        if owner_fd is not None:
            read_fds.append(owner_fd)
        readable, writable, _ = select.select(read_fds, [self.master] if self._output else [], [], timeout)
        # Receive controls before processing an already-ready command result.
        if self.master in readable:
            self._append(self.dispatcher.feed(os.read(self.master, RX_BYTES - 1)))
        if owner_fd in readable:
            self._append(self.dispatcher.poll())
        if self.master in writable:
            try:
                written = os.write(self.master, self._output)
                del self._output[:written]
            except BlockingIOError:
                pass

    def close(self, *, grace_seconds=1.0):
        if not self._closed:
            self._closed = True
            os.close(self.master)
            os.close(self.slave)
        return self.dispatcher.close(grace_seconds=grace_seconds)
