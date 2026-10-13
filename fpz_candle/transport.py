"""Opt-in USB transport. Importing/constructing this module never discovers USB."""

import fcntl
import os
import stat
from pathlib import Path

from .model import TransportError

VID, PID, INTERFACE = 0x140C, 0x0001, 0
READ_ENDPOINT, WRITE_ENDPOINT, PACKET_BYTES = 0x81, 0x02, 64
# Observed interrupt interval; codecs must wait between empty polls and impose
# their own overall response deadline. An empty packet does not complete a reply.
POLL_INTERVAL_SECONDS = 0.010


def validate_frame(frame: bytes) -> None:
    # A deliberately conservative bridge limit: one descriptor-sized packet.
    # This is not a claimed firmware command-length limit.
    if not isinstance(frame, bytes) or not 1 < len(frame) <= PACKET_BYTES:
        raise TransportError("A command must fit one 64-byte packet, including CR")
    if frame[-1:] != b"\r" or any(c < 0x20 or c > 0x7E for c in frame[:-1]):
        raise TransportError("Expected one printable ASCII command terminated by CR")


class CRFramer:
    """Accumulate API fragments until CR, without treating USB packets as lines."""

    def __init__(self):
        self.pending = b""

    def feed(self, fragment: bytes) -> list[bytes]:
        pending = self.pending + fragment
        frames = []
        try:
            while b"\r" in pending:
                frame, pending = pending.split(b"\r", 1)
                validate_frame(frame + b"\r")
                frames.append(frame + b"\r")
            if len(pending) >= PACKET_BYTES or any(c < 0x20 or c > 0x7E for c in pending):
                raise TransportError("Invalid or oversized partial command")
        except TransportError:
            self.pending = b""
            raise
        self.pending = pending
        return frames


class USBTransport:
    def __init__(self, *, bus: int, address: int, allow_io: bool = False,
                 timeout_ms: int = 1000, lock_dir: Path | None = None,
                 usb_modules=None):
        if not 1 <= bus <= 255 or not 1 <= address <= 127 or timeout_ms <= 0:
            raise ValueError("Explicit valid USB bus/address and finite timeout required")
        self.bus, self.address = bus, address
        self.allow_io, self.timeout_ms = allow_io, timeout_ms
        self.lock_dir = lock_dir
        self._modules = usb_modules  # Test injection; modules imported only in open().
        self._device = self._util = None
        self._lock_fd = None
        self._claimed = False
        self.faulted = False

    def _lock(self):
        directory = self.lock_dir
        if directory is None:
            runtime = os.environ.get("XDG_RUNTIME_DIR")
            if not runtime:
                raise TransportError("XDG_RUNTIME_DIR is required for exclusive USB ownership")
            directory = Path(runtime)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise TransportError("USB ownership lock directory must be private and user-owned")
        path = directory / f"fpz-usb-{self.bus}-{self.address}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise TransportError("Unsafe USB ownership lock file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(fd)
            raise
        self._lock_fd = fd

    def open(self) -> None:
        if self.faulted:
            raise TransportError("USB transport fault is latched; construct a new session")
        if not self.allow_io:
            raise TransportError("USB I/O is disabled")
        if self._claimed:
            return
        try:
            self._lock()
            if self._modules is None:
                import usb.core
                import usb.util
                self._modules = (usb.core, usb.util)
            core, self._util = self._modules
            devices = list(core.find(find_all=True, idVendor=VID, idProduct=PID,
                                     bus=self.bus, address=self.address))
            if len(devices) != 1:
                raise TransportError("Exact FPZ USB target is absent or ambiguous")
            self._device = devices[0]
            interface = self._device.get_active_configuration()[(INTERFACE, 0)]
            endpoints = {ep.bEndpointAddress: ep for ep in interface}
            for address, kind in ((READ_ENDPOINT, 3), (WRITE_ENDPOINT, 2)):
                ep = endpoints.get(address)
                if ep is None or ep.bmAttributes & 3 != kind or ep.wMaxPacketSize != PACKET_BYTES:
                    raise TransportError("FPZ endpoint descriptors differ from the verified layout")
            if self._device.is_kernel_driver_active(INTERFACE):
                raise TransportError("FPZ interface already has a kernel driver; refusing detach")
            # Never set_configuration(), reset(), clear_halt(), or detach a driver.
            self._util.claim_interface(self._device, INTERFACE)
            self._claimed = True
        except Exception as exc:
            self.faulted = True
            self.close()
            raise TransportError(f"USB open failed: {exc}") from exc

    def _require_open(self):
        if not self._claimed or self.faulted:
            raise TransportError("USB transport is not open or has faulted")

    def write_command(self, frame: bytes) -> None:
        validate_frame(frame)
        self._require_open()
        try:
            sent = self._device.write(WRITE_ENDPOINT, frame, timeout=self.timeout_ms)
            if sent != len(frame):
                raise TransportError("Short USB write; delivery state is unknown")
        except Exception as exc:
            self.faulted = True
            raise TransportError(f"USB write failed; no retry: {exc}") from exc

    def read_packet(self) -> bytes:
        self._require_open()
        try:
            packet = bytes(self._device.read(READ_ENDPOINT, PACKET_BYTES, timeout=self.timeout_ms))
            if len(packet) > PACKET_BYTES:
                raise TransportError("Invalid USB response length")
            return packet
        except Exception as exc:
            self.faulted = True
            raise TransportError(f"USB read failed; no retry: {exc}") from exc

    def close(self) -> None:
        # Release our claim only. No device stop/reset commands are inferred.
        try:
            if self._device is not None and self._util is not None:
                try:
                    if self._claimed:
                        self._util.release_interface(self._device, INTERFACE)
                finally:
                    self._util.dispose_resources(self._device)
        finally:
            self._claimed = False
            self._device = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
