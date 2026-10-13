import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import serial

from fpz_candle.backend import HardwareBackend, SimulationBackend
from fpz_candle.grbl import GRBLSession
from fpz_candle.model import MachineStatus, Receipt, TransportError
from fpz_candle.transport import CRFramer, USBTransport


class FramingTests(unittest.TestCase):
    def test_fragmented_cr_commands_and_batch(self):
        framer = CRFramer()
        self.assertEqual(framer.feed(b"AB"), [])
        self.assertEqual(framer.feed(b"1\rCD\rE"), [b"AB1\r", b"CD\r"])
        self.assertEqual(framer.pending, b"E")
        self.assertEqual(framer.feed(b"\r"), [b"E\r"])

    def test_invalid_batch_emits_nothing_and_discards_partial(self):
        for data in (b"A\rB\n", b"A\r\r", b"A" * 64, b"\x00"):
            framer = CRFramer()
            framer.feed(b"P")
            with self.assertRaises(TransportError):
                framer.feed(data)
            self.assertEqual(framer.pending, b"")


class USBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = Mock()
        self.device.get_active_configuration.return_value = {(0, 0): [
            SimpleNamespace(bEndpointAddress=0x81, bmAttributes=3, wMaxPacketSize=64),
            SimpleNamespace(bEndpointAddress=0x02, bmAttributes=2, wMaxPacketSize=64),
        ]}
        self.device.is_kernel_driver_active.return_value = False
        self.device.write.side_effect = lambda endpoint, data, **kw: len(data)
        self.device.read.return_value = b"R\r"
        self.core, self.util = Mock(), Mock()
        self.core.find.return_value = [self.device]

    def transport(self, allow_io=False):
        transport = USBTransport(bus=1, address=2, allow_io=allow_io,
                                 lock_dir=Path(self.tmp.name), usb_modules=(self.core, self.util))
        self.addCleanup(transport.close)
        return transport

    def test_constructor_and_disabled_open_never_discover_device(self):
        transport = self.transport()
        self.core.find.assert_not_called()
        with self.assertRaises(TransportError):
            transport.open()
        self.core.find.assert_not_called()
        self.util.claim_interface.assert_not_called()
        self.device.reset.assert_not_called()

    def test_exact_target_and_descriptors_and_packet_io(self):
        transport = self.transport(True)
        transport.open()
        self.core.find.assert_called_once_with(find_all=True, idVendor=0x140C,
                                              idProduct=1, bus=1, address=2)
        self.util.claim_interface.assert_called_once_with(self.device, 0)
        transport.write_command(b"TEST\r")
        self.assertEqual(transport.read_packet(), b"R\r")
        self.device.write.assert_called_once_with(0x02, b"TEST\r", timeout=1000)
        self.device.read.assert_called_once_with(0x81, 64, timeout=1000)
        for method in ("set_configuration", "reset", "clear_halt", "detach_kernel_driver"):
            getattr(self.device, method).assert_not_called()

    def test_competing_target_lock_fails_before_discovery(self):
        first, second = self.transport(True), self.transport(True)
        first.open()
        with self.assertRaises(TransportError):
            second.open()
        self.assertEqual(self.core.find.call_count, 1)
        first.close()
        third = self.transport(True)
        third.open()
        self.assertEqual(self.core.find.call_count, 2)

    def test_existing_kernel_driver_is_never_detached(self):
        self.device.is_kernel_driver_active.return_value = True
        transport = self.transport(True)
        with self.assertRaises(TransportError):
            transport.open()
        self.util.claim_interface.assert_not_called()
        self.device.detach_kernel_driver.assert_not_called()

    def test_wrong_descriptor_fails_before_claim(self):
        self.device.get_active_configuration.return_value[(0, 0)][0].bmAttributes = 2
        transport = self.transport(True)
        with self.assertRaises(TransportError):
            transport.open()
        self.util.claim_interface.assert_not_called()

    def test_short_write_and_timeout_latch_no_retry(self):
        for failure in ("short", "timeout"):
            with self.subTest(failure=failure):
                transport = self.transport(True)
                transport.open()
                if failure == "short":
                    self.device.write.side_effect = lambda *a, **kw: 1
                    with self.assertRaises(TransportError):
                        transport.write_command(b"TEST\r")
                else:
                    self.device.read.side_effect = TimeoutError("USB test timeout")
                    with self.assertRaises(TransportError):
                        transport.read_packet()
                count = self.device.write.call_count
                with self.assertRaises(TransportError):
                    transport.write_command(b"TEST\r")
                with self.assertRaises(TransportError):
                    transport.open()
                self.assertEqual(self.device.write.call_count, count)
                transport.close()

    def test_invalid_wire_frames_never_written(self):
        transport = self.transport(True)
        transport.open()
        for frame in (b"\r", b"A\rB\r", b"A\n", b"A" * 64 + b"\r", b"A"):
            with self.assertRaises(TransportError):
                transport.write_command(frame)
        self.device.write.assert_not_called()

    def test_zero_length_interrupt_packet_is_no_data_not_failure(self):
        self.device.read.side_effect = [b"", b"R\r"]
        transport = self.transport(True)
        transport.open()
        self.assertEqual(transport.read_packet(), b"")
        self.assertFalse(transport.faulted)
        self.assertEqual(transport.read_packet(), b"R\r")
        self.device.write.assert_not_called()


class SessionTests(unittest.TestCase):
    def simulation(self):
        backend = SimulationBackend()
        return GRBLSession(backend), backend

    def test_candle_handshake_never_enables_spindle(self):
        session, backend = self.simulation()
        result = session.feed(b"\x18S12000\r$G\r?").decode()
        self.assertTrue(result.startswith("Grbl 1.1h ['$' for help]\r\n"))
        self.assertIn("SIMULATION ONLY", result)
        self.assertIn("S12000]", result)
        self.assertEqual(result.splitlines().count("ok"), 2)
        self.assertIn("FS:0,0", result)
        self.assertEqual(backend.spindle, 0)

    def test_unavailable_handshake_has_no_fake_coordinates_or_io(self):
        transport = Mock()
        session = GRBLSession(HardwareBackend(transport))
        output = session.feed(b"\x18S1000\r$G\r?G1X1F10\r").decode()
        self.assertIn("<Alarm>", output)
        self.assertNotIn("MPos:", output)
        self.assertIn("error:20", output)
        self.assertEqual(output.splitlines().count("ok"), 2)
        transport.open.assert_not_called()
        transport.write_command.assert_not_called()

    def test_unit_and_distance_modes_then_run_idle(self):
        session, backend = self.simulation()
        self.assertEqual(session.feed(b"G20G90G1X1F2\rG91X.5\r"), b"ok\r\nok\r\n")
        first = session.feed(b"?").decode()
        self.assertIn("<Run|MPos:0.000,0.000,0.000", first)
        second = session.feed(b"?").decode()
        self.assertIn("<Idle|MPos:38.100,0.000,0.000", second)
        self.assertAlmostEqual(session.modes.feed_mm_min, 50.8)
        self.assertAlmostEqual(backend.position[0], 38.1)

    def test_jog_has_separate_modes_and_explicit_feed(self):
        session, _ = self.simulation()
        before = session.modes
        self.assertEqual(session.feed(b"$J=G21G91X2F10\r"), b"ok\r\n")
        self.assertEqual(session.modes, before)
        session.feed(b"?")
        self.assertIn(b"MPos:2.000,0.000,0.000", session.feed(b"?"))
        self.assertIn(b"error:16", session.feed(b"$J=G21G91X2\r"))

    def test_whole_invalid_block_is_rejected_without_mutation(self):
        session, backend = self.simulation()
        for line in ("G91G1X10F5M3M5", "G1X10F5Q1", "G1X10X20F5", "G90G91X1",
                     "G2X2I1", "G21G53G90X0Y0Z0", "G21G92X0Y0Z0", "G4P1", "M4"):
            with self.subTest(line=line):
                old = session.modes
                output = session.feed((line + "\r").encode()).decode().splitlines()
                self.assertTrue(any(s.startswith("error:") for s in output))
                self.assertNotIn("ok", output)
                self.assertEqual(session.modes, old)
                self.assertEqual(backend.planned_position, (0, 0, 0))

    def test_fragmented_lines_comments_crlf_and_overflow(self):
        session, _ = self.simulation()
        self.assertEqual(session.feed(b"g21g1x"), b"")
        self.assertEqual(session.feed(b"1f2(comment)\r\n"), b"ok\r\n")
        self.assertEqual(session.feed(b"A" * 128 + b"\r\n"), b"error:11\r\n")
        self.assertEqual(session.feed(b"S0;comment\n"), b"ok\r\n")

    def test_realtime_does_not_consume_ack_credit(self):
        session, _ = self.simulation()
        result = session.feed(b"G1X?1F10\r").decode()
        self.assertEqual(result.splitlines().count("ok"), 1)
        result = session.feed(b"!~\x85\x90").decode()
        self.assertNotIn("ok", result.splitlines())
        self.assertNotIn("error:", result)

    def test_partial_line_reset_discards_it(self):
        session, backend = self.simulation()
        session.feed(b"G1X100\x18F10\r??")
        self.assertEqual(backend.position, (0, 0, 0))

    def test_spindle_requires_explicit_m3_and_m5_stops_model(self):
        session, backend = self.simulation()
        session.feed(b"S1000\r")
        self.assertEqual(backend.spindle, 0)
        session.feed(b"M3\r")
        self.assertEqual(backend.spindle, 1000)
        session.feed(b"M5\r")
        self.assertEqual(backend.spindle, 0)

    def test_m2_m30_synchronize_stop_and_reset_only_selected_modes(self):
        for terminator in ("M2", "M30"):
            with self.subTest(terminator=terminator):
                session, backend = self.simulation()
                session.feed(b"G20G91G0X1F2S1000M3\r")
                self.assertTrue(backend.moving)
                result = session.feed((terminator + "\r").encode()).decode().splitlines()
                self.assertTrue(result[0].startswith("<Run|"))
                self.assertEqual(result[1:3], ["[MSG:Pgm End]", "ok"])
                self.assertTrue(result[3].startswith("<Idle|MPos:25.400"))
                self.assertIn("FS:0,0", result[3])
                self.assertFalse(backend.moving)
                self.assertEqual(backend.spindle, 0)
                self.assertEqual(session.modes.motion, "G1")
                self.assertEqual(session.modes.distance, "G90")
                self.assertEqual(session.modes.spindle, "M5")
                self.assertEqual(session.modes.units, "G20")
                self.assertAlmostEqual(session.modes.feed_mm_min, 50.8)
                self.assertEqual(session.modes.desired_rpm, 1000)
                self.assertIn("G1 G54 G17 G20 G90 G94 M5 M9 T0 F2 S1000", session.modes.report())

    def test_original_square_with_m2_completes_without_stripping(self):
        session, backend = self.simulation()
        job = (b"(square)\rG21 G90 G17 G94\rG0 X0 Y0 Z0\r"
               b"G1 X1 Y0 F100\rG1 X1 Y1\rG1 X0 Y1\rG1 X0 Y0\rM2\r")
        lines = session.feed(job).decode().splitlines()
        self.assertEqual(lines.count("ok"), 8)
        self.assertFalse(any(line.startswith("error:") for line in lines))
        run = next(i for i, line in enumerate(lines) if line.startswith("<Run|"))
        idle = next(i for i, line in enumerate(lines) if line.startswith("<Idle|"))
        last_ack = max(i for i, line in enumerate(lines) if line == "ok")
        self.assertLess(run, last_ack)
        self.assertLess(last_ack, idle)
        self.assertEqual(backend.position, (0, 0, 0))

    def test_program_end_follows_motion_in_same_block(self):
        session, backend = self.simulation()
        output = session.feed(b"G91G1X2F10S1000M3M30\r")
        self.assertNotIn(b"error:", output)
        self.assertEqual(backend.position, (2, 0, 0))
        self.assertEqual(backend.spindle, 0)
        self.assertEqual(session.modes.distance, "G90")
        self.assertIn(b"error:21", session.feed(b"M2M30\r"))

    def test_program_end_real_failure_rolls_back_and_never_acknowledges(self):
        unproven = [Receipt(True, False), Receipt(True, True),
                    Receipt(True, True, MachineStatus("Run", spindle_rpm=0, measured=True,
                                                      spindle_enabled=False)),
                    Receipt(True, True, MachineStatus("Idle", spindle_rpm=1000, measured=True,
                                                      spindle_enabled=False)),
                    Receipt(True, True, MachineStatus("Idle", spindle_rpm=0, measured=True)),
                    Receipt(True, True, MachineStatus("Idle", spindle_rpm=0, measured=False,
                                                      spindle_enabled=False))]
        for receipt in unproven:
            with self.subTest(receipt=receipt):
                codec = Mock()
                codec.execute.return_value = receipt
                session = GRBLSession(HardwareBackend(Mock(), codec))
                session.feed(b"G20G91G0F2S1000\r")
                before = session.modes
                result = session.feed(b"M2\r").decode().splitlines()
                self.assertNotIn("ok", result)
                self.assertNotIn("[MSG:Pgm End]", result)
                self.assertEqual(session.modes, before)
                self.assertEqual(codec.execute.call_args.args[0].program_end, "M2")

    def test_program_end_real_success_requires_idle_and_spindle_disabled(self):
        codec = Mock()
        status = MachineStatus("Idle", (1, 2, 3), 0, 0, measured=True, spindle_enabled=False)
        codec.execute.return_value = Receipt(True, True, status)
        session = GRBLSession(HardwareBackend(Mock(), codec))
        session.feed(b"G20G91G0F2S1000\r")
        result = session.feed(b"M30\r").decode().splitlines()
        self.assertEqual(result[:2], ["[MSG:Pgm End]", "ok"])
        self.assertEqual(session.modes.distance, "G90")
        self.assertEqual(session.modes.units, "G20")
        self.assertEqual(session.modes.desired_rpm, 1000)
        self.assertEqual(codec.execute.call_args.args[0].program_end, "M30")

    def test_active_spindle_s_word_requires_new_hardware_receipt(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                codec = Mock()
                codec.execute.return_value = Receipt(True, True)
                session = GRBLSession(HardwareBackend(Mock(), codec))
                self.assertEqual(session.feed(b"M3S1000\r"), b"ok\r\n")
                if fail:
                    codec.execute.side_effect = TransportError("speed write failed")
                result = session.feed(b"S0\r")
                self.assertEqual(codec.execute.call_count, 2)
                if fail:
                    self.assertNotIn(b"ok", result)
                    self.assertEqual(session.modes.desired_rpm, 1000)
                else:
                    self.assertEqual(result, b"ok\r\n")
                    self.assertEqual(session.modes.desired_rpm, 0)

    def test_hold_rejects_new_motion_without_corrupting_resume(self):
        session, backend = self.simulation()
        result = session.feed(b"G1X1F10\r!G1X2\r~??").decode()
        self.assertIn("error:9", result)
        self.assertNotIn("Alarm", result)
        self.assertIsNone(session.fault)
        self.assertEqual(backend.position, (1, 0, 0))

    def test_latched_fault_does_not_resume_held_model(self):
        session, backend = self.simulation()
        session.feed(b"G1X1F10\r!")
        session.fault = "execution result unknown"
        result = session.feed(b"~??").decode()
        self.assertIn("Alarm", result)
        self.assertTrue(backend.hold)
        self.assertEqual(backend.position, (0, 0, 0))

    def test_unknown_codec_program_end_never_opens_usb(self):
        transport = Mock()
        session = GRBLSession(HardwareBackend(transport))
        session.feed(b"G91\r")
        before = session.modes
        result = session.feed(b"M2\r")
        self.assertIn(b"error:20", result)
        self.assertNotIn(b"ok", result)
        self.assertEqual(session.modes, before)
        transport.open.assert_not_called()

    def test_failed_hardware_never_acknowledges_or_changes_modal_state(self):
        transport, codec = Mock(), Mock()
        codec.execute.side_effect = TransportError("short write")
        backend = HardwareBackend(transport, codec)
        session = GRBLSession(backend)
        before = session.modes
        result = session.feed(b"G91G1X1F10\rG1X2F10\r\x18?").decode()
        self.assertNotIn("ok", result.splitlines())
        self.assertNotIn("Grbl", result)
        self.assertIn("<Alarm>", result)
        self.assertNotIn("MPos:", result)
        self.assertEqual(codec.execute.call_count, 1)
        self.assertEqual(session.modes, before)

    def test_usb_acceptance_is_not_motor_completion(self):
        for receipt in (Receipt(True, False), Receipt(False, True),
                        Receipt(True, True, MachineStatus("Idle", (1, 2, 3)))):
            with self.subTest(receipt=receipt):
                codec = Mock()
                codec.execute.return_value = receipt
                session = GRBLSession(HardwareBackend(Mock(), codec))
                output = session.feed(b"G1X1F10\r?").decode()
                self.assertNotIn("ok", output.splitlines())
                self.assertIn("error:9", output)
                self.assertNotIn("MPos:", output)

    def test_only_codec_measured_position_is_exposed(self):
        codec = Mock()
        measured = MachineStatus("Idle", (3.1, 4.2, 5.3), 0, 0, measured=True)
        codec.execute.return_value = Receipt(True, True, measured)
        codec.status.return_value = measured
        session = GRBLSession(HardwareBackend(Mock(), codec))
        self.assertEqual(session.feed(b"G1X99F10\r"), b"ok\r\n")
        output = session.feed(b"?").decode()
        self.assertIn("MPos:3.100,4.200,5.300", output)
        self.assertNotIn("99.000", output)
        codec.status.side_effect = TimeoutError("status missing")
        self.assertIn(b"<Alarm>", session.feed(b"?"))
        self.assertNotIn(b"ok", session.feed(b"G1X1F10\r"))

    def test_unproven_hold_blocks_future_motion_without_claiming_stop(self):
        codec = Mock()
        codec.execute.return_value = Receipt(True, True)
        backend = HardwareBackend(Mock(), codec)
        session = GRBLSession(backend)
        session.feed(b"G1X1F10\r")
        result = session.feed(b"!G1X2F10\r\x18").decode()
        self.assertNotIn("Hold:", result)
        self.assertNotIn("ok", result.splitlines())
        self.assertEqual(codec.execute.call_count, 1)
        codec.realtime.assert_not_called()


class PTYTests(unittest.TestCase):
    def test_cli_pyserial_candle_exchange_and_reconnect(self):
        process = subprocess.Popen([sys.executable, "-m", "fpz_candle", "--mode", "simulation"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "PTY not ready")
            path = process.stdout.readline().strip()
            self.assertTrue(path.startswith("/dev/pts/"))
            for reconnect in range(2):
                with serial.Serial(path, baudrate=115200, timeout=1) as port:
                    port.reset_input_buffer()
                    port.write(b"\x18")
                    self.assertTrue(port.readline().startswith(b"Grbl 1.1h"))
                    self.assertIn(b"SIMULATION ONLY", port.readline())
                    port.write(b"S5000\r$G\r?")
                    lines = [port.readline() for _ in range(4)]
                    self.assertEqual(lines.count(b"ok\r\n"), 2)
                    self.assertTrue(any(b"FS:0,0" in line for line in lines))
                    port.write(b"G1X1F10\r")
                    self.assertEqual(port.readline(), b"ok\r\n")
                    port.write(b"?")
                    self.assertTrue(port.readline().startswith(b"<Run|"))
                    port.write(b"?")
                    self.assertTrue(port.readline().startswith(b"<Idle|"))
        finally:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("SIMULATION ONLY", stderr)


if __name__ == "__main__":
    unittest.main()
