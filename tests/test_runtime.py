"""Offline process/PTY tests. Fake codecs never discover or open USB devices."""

import multiprocessing
import os
import select
import subprocess
import sys
import time
import unittest

import serial

from fpz_candle.backend import HardwareBackend, SimulationBackend
from fpz_candle.model import MachineStatus, Receipt, TransportError
from fpz_candle.runtime import PTYServer, ResponsiveDispatcher


class FakeTransport:
    def __init__(self, events):
        self.events = events

    def open(self):
        self.events.put(("open", os.getpid()))

    def close(self):
        self.events.put(("close", os.getpid()))


class BlockingCodec:
    def __init__(self, entered, release, events, *, fail=False, status_entered=None, status_release=None):
        self.entered, self.release, self.events = entered, release, events
        self.fail = fail
        self.status_entered, self.status_release = status_entered, status_release

    def validate(self, command):
        pass

    def execute(self, command, transport):
        self.events.put(("execute", os.getpid(), command.source, command.modes.distance))
        self.entered.set()
        self.release.wait()  # Deliberately unbounded fake to test forced shutdown.
        if self.fail:
            raise TransportError("synthetic USB failure")
        status = MachineStatus("Idle", (1, 2, 3), 0, 0, measured=True, spindle_enabled=False)
        return Receipt(True, True, status)

    def status(self, transport):
        self.events.put(("status", os.getpid()))
        if self.status_entered is not None:
            self.status_entered.set()
            self.status_release.wait()
        return MachineStatus("Idle", (1, 2, 3), 0, 0, measured=True)

    def realtime(self, action, transport):
        raise AssertionError("No physical realtime operation is authorized")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.context = multiprocessing.get_context("spawn")

    def fake(self, **options):
        entered, release, events = self.context.Event(), self.context.Event(), self.context.SimpleQueue()
        codec = BlockingCodec(entered, release, events, **options)
        backend = HardwareBackend(FakeTransport(events), codec)
        self.addCleanup(events.close)
        return backend, entered, release, events

    def dispatcher(self, backend):
        dispatch = ResponsiveDispatcher(backend)
        self.addCleanup(dispatch.close, grace_seconds=0.1)
        return dispatch

    def drain(self, dispatch, *, timeout=3):
        output = bytearray()
        deadline = time.monotonic() + timeout
        while dispatch.pending:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "backend result did not arrive")
            select.select([dispatch.fileno()], [], [], remaining)
            output.extend(dispatch.poll())
        return bytes(output)

    def events(self, events):
        found = []
        while events._reader.poll(0.05):
            found.append(events.get())
        return found

    def test_reset_before_open_and_candle_startup(self):
        backend, _, _, events = self.fake()
        dispatch = self.dispatcher(backend)
        self.assertEqual(dispatch.feed(b"\x18"), b"")
        self.assertIn(b"Grbl 1.1h", self.drain(dispatch))
        result = dispatch.feed(b"S0\r$G\r?") + self.drain(dispatch)
        self.assertEqual(result.splitlines().count(b"ok"), 2)
        self.assertIn(b"<Alarm>", result)
        self.assertEqual(self.events(events), [])

    def test_queued_lines_parse_only_after_matching_modal_commit(self):
        backend, entered, release, events = self.fake()
        dispatch = self.dispatcher(backend)
        self.assertEqual(dispatch.feed(b"G91G1X1F10\rX2\r$G\r"), b"")
        self.assertTrue(entered.wait(3))
        self.assertEqual(dispatch.session.modes.distance, "G90")
        release.set()
        output = self.drain(dispatch)
        self.assertEqual(output.splitlines().count(b"ok"), 3)
        self.assertIn(b"G91", output)
        calls = [e for e in self.events(events) if e[0] == "execute"]
        self.assertEqual([e[2:] for e in calls], [("G91G1X1F10", "G91"), ("X2", "G91")])
        self.assertEqual(len({e[1] for e in calls}), 1)
        self.assertNotEqual(calls[0][1], os.getpid())
        self.assertEqual(dispatch.buffered_bytes, 0)

    def test_status_and_controls_during_blocked_command_no_concurrent_io_or_late_ack(self):
        for control in (b"!", b"\x18", b"\x85"):
            with self.subTest(control=control):
                backend, entered, release, events = self.fake()
                dispatch = self.dispatcher(backend)
                dispatch.feed(b"G91G1X1F10\rX2\r")
                self.assertTrue(entered.wait(3))
                status = dispatch.feed(b"?")
                self.assertIn(b"physical state unknown", status)
                self.assertNotIn(b"MPos", status)
                self.assertNotIn(b"ok", status.splitlines())
                rejected = dispatch.feed(control)
                self.assertEqual(rejected.splitlines().count(b"error:9"), 2)
                self.assertNotIn(b"Hold:", rejected)
                self.assertNotIn(b"Grbl", rejected)
                self.assertFalse(release.is_set())  # Controls answered while codec remains blocked.
                release.set()
                self.assertEqual(self.drain(dispatch), b"")
                self.assertEqual(dispatch.session.modes.distance, "G90")
                calls = self.events(events)
                self.assertEqual(sum(e[0] == "execute" for e in calls), 1)
                self.assertFalse(any(e[0] == "status" for e in calls))
                self.assertNotIn(b"ok", dispatch.feed(b"G1X3F10\r").splitlines())
                dispatch.close()

    def test_failed_command_rejects_queued_execution_in_order(self):
        backend, entered, release, events = self.fake(fail=True)
        dispatch = self.dispatcher(backend)
        dispatch.feed(b"G91G1X1F10\rX2\rX3\r")
        self.assertTrue(entered.wait(3))
        release.set()
        output = self.drain(dispatch)
        self.assertEqual(output.splitlines().count(b"error:9"), 3)
        self.assertNotIn(b"ok", output.splitlines())
        self.assertEqual(dispatch.session.modes.distance, "G90")
        self.assertEqual(sum(e[0] == "execute" for e in self.events(events)), 1)

    def test_bounded_window_includes_pending_queued_and_partial(self):
        backend, entered, release, _ = self.fake()
        dispatch = self.dispatcher(backend)
        first = b"G1X1F10\r"
        dispatch.feed(first)
        self.assertTrue(entered.wait(3))
        self.assertEqual(dispatch.feed(b"A" * (127 - len(first))), b"")
        self.assertEqual(dispatch.buffered_bytes, 127)
        output = dispatch.feed(b"A" * 1024 + b"?\r")
        self.assertIn(b"error:11", output)
        self.assertEqual(output.splitlines().count(b"error:9"), 1)
        self.assertLessEqual(dispatch.buffered_bytes, 127)
        self.assertNotIn(b"MPos", output)
        release.set()
        self.assertEqual(self.drain(dispatch), b"")

    def test_fragmented_crlf_credit_and_realtime_do_not_create_extra_ack(self):
        dispatch = self.dispatcher(SimulationBackend())
        self.assertEqual(dispatch.feed(b"G21S"), b"")
        output = dispatch.feed(b"0\r\n$G\n")
        self.assertEqual(output.splitlines().count(b"ok"), 2)
        dispatch.feed(b"??")
        result = self.drain(dispatch)
        self.assertIn(b"<Idle|", result)
        self.assertNotIn(b"ok", result.splitlines())

    def test_pending_status_also_has_single_owner_and_can_be_invalidated(self):
        status_entered, status_release = self.context.Event(), self.context.Event()
        backend, entered, release, events = self.fake(status_entered=status_entered,
                                                    status_release=status_release)
        release.set()
        dispatch = self.dispatcher(backend)
        dispatch.feed(b"G1X1F10\r")
        self.drain(dispatch)
        dispatch.feed(b"?")
        self.assertTrue(status_entered.wait(3))
        output = dispatch.feed(b"!G1X2F10\r")
        self.assertEqual(output.splitlines().count(b"error:9"), 1)
        self.assertNotIn(b"MPos", output)
        status_release.set()
        self.assertEqual(self.drain(dispatch), b"")
        self.assertEqual(sum(e[0] == "execute" for e in self.events(events)), 1)

    def test_worker_failure_credits_pending_and_queued_once(self):
        backend, entered, _, _ = self.fake()
        dispatch = self.dispatcher(backend)
        dispatch.feed(b"G1X1F10\rX2\r")
        self.assertTrue(entered.wait(3))
        dispatch._owner.terminate()
        dispatch._owner.join(3)
        output = dispatch.poll()
        self.assertEqual(output.splitlines().count(b"error:9"), 2)
        self.assertEqual(dispatch.poll(), b"")
        self.assertNotIn(b"ok", dispatch.feed(b"G1X3F10\r").splitlines())

    def test_shutdown_is_bounded_and_never_concurrently_closes_transport(self):
        backend, entered, _, events = self.fake()
        dispatch = self.dispatcher(backend)
        dispatch.feed(b"G91G1X1F10\r")
        self.assertTrue(entered.wait(3))
        shutdown = dispatch.close(grace_seconds=0.05)
        self.assertTrue(shutdown.owner_exited)
        self.assertTrue(shutdown.forced)
        self.assertTrue(shutdown.operation_abandoned)
        self.assertFalse(shutdown.clean)
        self.assertFalse(dispatch._owner.is_alive())
        self.assertEqual(dispatch.session.modes.distance, "G90")
        self.assertFalse(any(e[0] == "close" for e in self.events(events)))
        self.assertEqual(dispatch.close(), shutdown)
        with self.assertRaises(RuntimeError):
            dispatch.feed(b"G0X1\r")

    def test_normal_close_executes_only_in_same_owner(self):
        backend, _, release, events = self.fake()
        release.set()
        dispatch = self.dispatcher(backend)
        dispatch.feed(b"G1X1F10\r")
        self.drain(dispatch)
        result = dispatch.close()
        self.assertTrue(result.clean)
        calls = self.events(events)
        self.assertEqual([e[0] for e in calls], ["open", "execute", "close"])
        self.assertEqual(len({e[1] for e in calls}), 1)
        self.assertNotEqual(calls[0][1], os.getpid())

    def test_real_pty_receives_realtime_while_codec_is_blocked(self):
        backend, entered, release, events = self.fake()
        server = PTYServer(backend)
        self.addCleanup(server.close, grace_seconds=0.1)
        with serial.Serial(server.path, 115200, timeout=0) as port:
            port.reset_input_buffer()
            port.write(b"G91G1X1F10\rX2\r")
            deadline = time.monotonic() + 3
            while not entered.is_set() and time.monotonic() < deadline:
                server.step(0.01)
            self.assertTrue(entered.is_set())
            port.read(4096)
            port.write(b"?!\x18")
            response = bytearray()
            while response.splitlines().count(b"error:9") < 2 and time.monotonic() < deadline:
                server.step(0.01)
                response.extend(port.read(4096))
            self.assertEqual(response.splitlines().count(b"error:9"), 2)
            self.assertIn(b"physical state unknown", response)
            self.assertNotIn(b"MPos", response)
            self.assertFalse(release.is_set())
            release.set()
            while server.dispatcher.pending and time.monotonic() < deadline:
                server.step(0.01)
            self.assertFalse(server.dispatcher.pending)
            self.assertEqual(server.dispatcher.session.modes.distance, "G90")
            self.assertEqual(sum(e[0] == "execute" for e in self.events(events)), 1)

    def test_cli_original_m2_job_with_250ms_status_polling(self):
        process = subprocess.Popen([sys.executable, "-m", "fpz_candle", "--mode", "simulation"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            path = process.stdout.readline().strip()
            with serial.Serial(path, 115200, timeout=0.05) as port:
                port.reset_input_buffer()
                port.write(b"\x18")
                initial = bytearray()
                deadline = time.monotonic() + 5
                while b"SIMULATION ONLY" not in initial and time.monotonic() < deadline:
                    initial.extend(port.read(4096))
                self.assertIn(b"Grbl 1.1h", initial)
                port.write(b"(square)\rG21G90G17G94\rG0X0Y0Z0\rG1X1Y0F100\r"
                           b"G1X1Y1\rG1X0Y1\rG1X0Y0\rM2\r")
                output = bytearray()
                next_poll = 0
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if now >= next_poll:
                        port.write(b"?")
                        next_poll = now + 0.25
                    output.extend(port.read(4096))
                    lines = output.splitlines()
                    if lines.count(b"ok") == 8 and any(x.startswith(b"<Idle|") for x in lines):
                        break
                self.assertEqual(output.splitlines().count(b"ok"), 8, output)
                self.assertIn(b"[MSG:Pgm End]", output)
                self.assertIn(b"<Run|", output)
                self.assertNotIn(b"error:", output)
                self.assertNotIn(b"<Alarm>", output)
        finally:
            process.terminate()
            try:
                _, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
        self.assertEqual(process.returncode, 0, stderr)


if __name__ == "__main__":
    unittest.main()
