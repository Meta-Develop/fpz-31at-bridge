"""Synthetic protocol cases only: never open USB or load private settings."""

import unittest

from fpz_candle.fpz_protocol import (
    ERROR_CODES, PendingContext, ProtocolError, ResponseDeadline, StreamDecoder,
    absolute_xy_counts, completion_barrier, feed_controller_units,
    position_request, relative_xy_counts, request_position, spindle_off,
)
from fpz_candle.model import TransportError


class EncodingTests(unittest.TestCase):
    def test_exact_command_sequence(self):
        self.assertEqual([
            position_request(), completion_barrier(), relative_xy_counts(-1, 2),
            absolute_xy_counts(3, -4), spindle_off(), feed_controller_units(5),
        ], [b"RP\r", b"C\r", b"M -1,2\r", b"MA 3,-4\r", b"R0\r", b"S 5\r"])

    def test_no_coercion_or_injection(self):
        for value in (True, 1.0, "1\rC", 2**31, -2**31-1, None):
            for encoder in (feed_controller_units, lambda v: relative_xy_counts(v, 0),
                            lambda v: absolute_xy_counts(0, v)):
                with self.subTest(value=value, encoder=encoder):
                    with self.assertRaises(ValueError):
                        encoder(value)
        self.assertEqual(relative_xy_counts(-(2**31), 2**31-1), b"M -2147483648,2147483647\r")


class DecoderTests(unittest.TestCase):
    def test_all_split_boundaries_and_coalescing(self):
        raw = b"\x13R-12,34,56\r\x11X1 2 3\0Z4,5,6\rF"
        expected = StreamDecoder().feed(raw, context=PendingContext.POSITION)
        self.assertEqual([e.kind for e in expected], ["xoff", "position", "xon", "position", "position", "f_response"])
        self.assertEqual([e.counts for e in expected if e.kind == "position"], [(-12,34,56),(1,2,3),(4,5,6)])
        for split in range(len(raw)+1):
            decoder = StreamDecoder()
            self.assertEqual(decoder.feed(raw[:split], context=PendingContext.POSITION)
                             + decoder.feed(b"", context=PendingContext.POSITION)
                             + decoder.feed(raw[split:], context=PendingContext.POSITION), expected)
        decoder = StreamDecoder()
        events = []
        for byte in raw:
            events.extend(decoder.feed(bytes([byte]), context=PendingContext.POSITION))
        self.assertEqual(events, expected)

    def test_flow_inside_fragment_and_explicit_f_context(self):
        decoder = StreamDecoder()
        self.assertEqual(decoder.feed(b"R1,\x13")[0].kind, "xoff")
        events = decoder.feed(b"2,\x113\r")
        self.assertEqual([e.kind for e in events], ["xon", "position"])
        self.assertEqual(events[1].counts, (1,2,3))
        self.assertEqual(decoder.feed(b"F")[0].kind, "unsolicited_f")
        for context in PendingContext:
            event = decoder.feed(b"F", context=context)[0]
            self.assertEqual((event.kind, event.context, event.counts), ("f_response", context, None))

    def test_errors_are_distinct_from_positions_and_completion(self):
        for char, code in ERROR_CODES.items():
            event = StreamDecoder().feed(char.encode())[0]
            self.assertEqual((event.kind, event.application_error), ("error", code))
        self.assertEqual(StreamDecoder().feed(b"?")[0].kind, "unknown")
        self.assertEqual(StreamDecoder().feed(b""), [])

    def test_invalid_payload_latches_failure(self):
        for raw in (b"R\r", b"R1,2\r", b"R1,2,3,4\r", b"R2147483648,0,0\r",
                    b"R1,2,F", b"R1,2,3\n", b"R1,--2,3\r"):
            decoder = StreamDecoder()
            with self.subTest(raw=raw):
                with self.assertRaises(ProtocolError): decoder.feed(raw)
                with self.assertRaises(ProtocolError): decoder.feed(b"R1,2,3\r")
        with self.assertRaises(ProtocolError): StreamDecoder(max_payload_bytes=4).feed(b"R12345")
        decoder = StreamDecoder()
        decoder.feed(b"R1", context=PendingContext.POSITION)
        with self.assertRaises(ProtocolError): decoder.feed(b",2,3\r", context=PendingContext.BARRIER)


class FakeClock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def sleep(self, amount): self.now += amount


class FakeTransport:
    def __init__(self, packets=(), *, clock=None, operation_seconds=0):
        self.timeout_ms = 1000
        self.packets = iter(packets)
        self.writes = []
        self.timeouts = []
        self.clock = clock
        self.operation_seconds = operation_seconds
        self.faulted = False

    def write_command(self, frame):
        self.writes.append(frame)
        self.timeouts.append(self.timeout_ms)
        if self.clock: self.clock.now += self.operation_seconds

    def read_packet(self):
        self.timeouts.append(self.timeout_ms)
        if self.clock: self.clock.now += self.operation_seconds
        result = next(self.packets, b"")
        if isinstance(result, Exception): raise result
        return result


class TransactionTests(unittest.TestCase):
    def query(self, transport, clock, **kwargs):
        return request_position(transport, clock=clock, sleep=clock.sleep, **kwargs)

    def test_zlp_fragmentation_and_f_are_not_fault_or_completion(self):
        clock = FakeClock()
        transport = FakeTransport([b"", b"F", b"\x13R1,", b"", b"2,3\r\x11"])
        result = self.query(transport, clock)
        self.assertEqual(result.counts, (1,2,3))
        self.assertEqual([e.kind for e in result.responses], ["f_response", "xoff", "position", "xon"])
        self.assertEqual(transport.writes, [b"RP\r"])
        self.assertEqual(transport.timeout_ms, 1000)
        self.assertFalse(transport.faulted)

    def test_overall_deadline_covers_zlp_partial_and_f(self):
        for packets in ([], [b"F"]*20, [b"R1,"]):
            clock = FakeClock()
            transport = FakeTransport(packets)
            with self.assertRaises(ResponseDeadline): self.query(transport, clock, deadline_ms=35)
            self.assertLessEqual(clock.now, 0.036)
            self.assertEqual(transport.writes, [b"RP\r"])
            self.assertTrue(all(0 < n <= 35 for n in transport.timeouts))
            self.assertEqual(transport.timeout_ms, 1000)

    def test_errors_and_ambiguous_coalesced_data_reject_position(self):
        for packet in (b"L", b"R1,2,3\rL", b"R1,2,3\r?", b"R1,2,3\rX4,5,6\r", b"R1,2,3\rX"):
            clock = FakeClock()
            transport = FakeTransport([packet])
            with self.subTest(packet=packet):
                with self.assertRaises(ProtocolError): self.query(transport, clock)
                self.assertEqual(transport.writes, [b"RP\r"])
                self.assertEqual(transport.timeout_ms, 1000)

    def test_late_reply_and_write_consume_deadline(self):
        clock = FakeClock()
        transport = FakeTransport([b"R1,2,3\r"], clock=clock, operation_seconds=0.06)
        with self.assertRaises(ResponseDeadline): self.query(transport, clock, deadline_ms=100)
        self.assertLessEqual(transport.timeouts[1], 40)
        self.assertEqual(transport.timeout_ms, 1000)

    def test_transport_error_no_retry_timeout_restored(self):
        clock = FakeClock()
        transport = FakeTransport([TransportError("failed")])
        with self.assertRaises(TransportError): self.query(transport, clock)
        self.assertEqual(transport.writes, [b"RP\r"])
        self.assertEqual(transport.timeout_ms, 1000)

    def test_missing_timeout_or_invalid_deadline_rejected_before_write(self):
        transport = FakeTransport()
        for deadline in (0, -1, 1.5, True):
            with self.assertRaises(ValueError): request_position(transport, deadline_ms=deadline)
        del transport.timeout_ms
        with self.assertRaises(ValueError): request_position(transport)
        self.assertEqual(transport.writes, [])


if __name__ == "__main__":
    unittest.main()
