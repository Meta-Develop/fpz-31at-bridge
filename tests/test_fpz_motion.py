"""Pure planner tests; synthetic profiles, no USB, GUI, or private file loads."""

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction as Q
import unittest
from unittest.mock import patch

from fpz_candle.fpz_motion import AxisMapping, PlanningError, XYProfile, plan_xy, round_count
from fpz_candle.gcode import parse_block
from fpz_candle.model import Command, ModalState, Unsupported


def profile(**changes):
    # Explicit test fixture, never an installed machine calibration/default.
    return replace(XYProfile((AxisMapping(0, 1, Q(1, 10)), AxisMapping(1, 1, Q(1, 10))),
                             (0, 0), ((-1000, 1000), (-1000, 1000)), 1, 20, Q(11, 2)), **changes)


def command(source, **modes):
    return parse_block(source, replace(ModalState(), motion="G1", feed_mm_min=150, **modes))


class MotionTests(unittest.TestCase):
    def test_capture_scale_is_explicit_and_signed(self):
        p = profile(axes=(AxisMapping(0, 1, Decimal("0.00635")), AxisMapping(1, 1, Decimal("0.00635"))))
        for sign in (-1, 1):
            plan = plan_xy(command(f"G91 X{sign}"), measured_counts=(0, 8), profile=p)
            self.assertEqual(plan.frames, (b"S 2\r", f"M {sign*157},0\r".encode()))
            self.assertEqual(plan.effective_work_mm[0], sign * Q(19939, 20000))
            self.assertEqual(plan.target_counts, (sign*157, 8))

    def test_half_away_and_neighbors_exact(self):
        tiny = Q(1, 10**20)
        for n, expected in ((Q(1,2), 1), (-Q(1,2), -1), (Q(5,2), 3), (-Q(5,2), -3),
                            (Q(1,2)-tiny, 0), (-Q(1,2)+tiny, 0), (Q(1,2)+tiny, 1)):
            self.assertEqual(round_count(n), expected)
        for n in (float("nan"), float("inf"), Decimal("NaN"), True, 2**31, -2**31-1):
            with self.assertRaises(PlanningError): round_count(n)
        self.assertEqual(round_count(-2**31), -2**31)
        self.assertEqual(round_count(2**31-1), 2**31-1)

    def test_absolute_mapping_origin_and_retained_axis(self):
        p = profile(axes=(AxisMapping(1, -1, Q(1, 2)), AxisMapping(0, 1, Q(1, 4))), origin_counts=(100, 200))
        plan = plan_xy(command("G90 X1.25"), measured_counts=(117, 204), profile=p)
        self.assertEqual(plan.target_counts, (117, 197))
        self.assertEqual(plan.delta_counts, (0, -7))
        self.assertEqual(plan.requested_work_mm, (Q(5,4), Q(17,4)))
        self.assertEqual(plan.effective_work_mm, (Q(3,2), Q(17,4)))
        self.assertEqual(plan.frames[1], b"MA 117,197\r")

    def test_relative_rounds_delta_not_target(self):
        p = profile(axes=(AxisMapping(0, 1, 1), AxisMapping(1, 1, 1)))
        plan = plan_xy(command("G91 X-0.5"), measured_counts=(10, 5), profile=p)
        self.assertEqual(plan.target_counts, (9, 5))
        self.assertEqual(plan.frames[1], b"M -1,0\r")

    def test_inches_and_mm_have_identical_exact_targets_and_feed(self):
        inch = plan_xy(command("G20 G90 X1 Y-0.5 F10"), measured_counts=(0,0), profile=profile())
        mm = plan_xy(command("G21 G90 X25.4 Y-12.7 F254"), measured_counts=(0,0), profile=profile())
        self.assertEqual(inch, mm)
        self.assertEqual(inch.target_counts, (254, -127))
        self.assertEqual(inch.requested_feed_mm_s, Q(127,30))
        self.assertEqual(inch.requested_feed_mm_min, 254)
        self.assertEqual(inch.effective_feed_mm_s, 4)
        self.assertEqual(inch.effective_feed_mm_min, 240)
        # Retained feed is canonical mm/min even when axes are in inches.
        retained = plan_xy(command("G20 G91 X0.1"), measured_counts=(0,0), profile=profile())
        self.assertEqual(retained.controller_feed, 2)

    def test_g0_uses_separate_profile_speed(self):
        plan = plan_xy(command("G0 X1 F60"), measured_counts=(0,0), profile=profile())
        self.assertEqual((plan.requested_feed_mm_s, plan.controller_feed, plan.effective_feed_mm_min), (Q(11,2),5,300))
        self.assertEqual(plan.frames, (b"S 5\r", b"MA 10,0\r"))

    def test_feed_truncation_bounds_and_no_s_zero(self):
        for f, speed in ((150,2),(330,5),(474,7)):
            self.assertEqual(plan_xy(command(f"X1 F{f}"), measured_counts=(0,0), profile=profile()).controller_feed, speed)
        for p, f in ((profile(min_feed_mm_s=Q(1,10)), 30),
                     (profile(min_feed_mm_s=Q(3,2)), 100),
                     (profile(), 1201), (profile(), 0)):
            cmd = Command("gcode", "", (("X",1),("F",f)), replace(ModalState(), motion="G1", feed_mm_min=f))
            with self.assertRaises(PlanningError): plan_xy(cmd, measured_counts=(0,0), profile=p)

    def test_limits_include_start_requested_and_quantized_end(self):
        for cmd, start in ((command("X100.01"),(0,0)), (command("X0"),(1001,0)),
                           (command("G91 X0.1"),(1000,0))):
            with self.assertRaises(PlanningError): plan_xy(cmd, measured_counts=start, profile=profile())
        self.assertEqual(plan_xy(command("X100"), measured_counts=(0,0), profile=profile()).target_counts[0], 1000)

    def test_signed32_delta_and_target_overflow(self):
        p = profile(axes=(AxisMapping(0,1,1),AxisMapping(1,1,1)), travel_limits_counts=((-2**31,2**31-1),(-10,10)))
        for cmd, start in ((command("G91 X1"),(2**31-1,0)),
                           (command("G90 X2147483647"),(-2**31,0))):
            with self.assertRaises(PlanningError): plan_xy(cmd, measured_counts=start, profile=p)

    def test_unsupported_semantics_never_reach_encoders(self):
        bad = [command("X1 Z0"), command("X1 M5"), command("X1 S0"), command("X1 M2"),
               command("F150"), command("X1 M3")]
        base = command("X1")
        bad.extend([replace(base, words=(("G",2),("X",1))), replace(base, program_end="M30"),
                    replace(base, words=(("X",1),("Q",0)))])
        for cmd in bad:
            with patch("fpz_candle.fpz_motion.feed_controller_units") as feed, patch("fpz_candle.fpz_motion.absolute_xy_counts") as absolute:
                with self.assertRaises(Unsupported): plan_xy(cmd, measured_counts=(0,0), profile=profile())
                feed.assert_not_called()
                absolute.assert_not_called()

    def test_invalid_second_axis_or_feed_is_all_or_nothing(self):
        for cmd in (command("X1 Y101"), command("X1 F1201"),
                    replace(command("X1"), words=(("X",1),("Y",float("nan"))))):
            with patch("fpz_candle.fpz_motion.feed_controller_units") as feed, patch("fpz_candle.fpz_motion.absolute_xy_counts") as absolute:
                with self.assertRaises(PlanningError): plan_xy(cmd, measured_counts=(0,0), profile=profile())
                feed.assert_not_called()
                absolute.assert_not_called()

    def test_bad_profiles_and_ambiguous_inputs_rejected(self):
        for p in (profile(axes=(AxisMapping(0,1,1),AxisMapping(0,1,1))),
                  profile(axes=(AxisMapping(0,0,1),AxisMapping(1,1,1))),
                  profile(axes=(AxisMapping(0,1,0),AxisMapping(1,1,1))),
                  profile(rapid_mm_s=0), profile(max_feed_mm_s=float("inf")),
                  profile(travel_limits_counts=((1,-1),(0,10)))):
            with self.assertRaises(PlanningError): plan_xy(command("X1"), measured_counts=(0,0), profile=p)
        for start in ((True,0),(0,),[0,0],(2**31,0)):
            with self.assertRaises(PlanningError): plan_xy(command("X1"), measured_counts=start, profile=profile())


if __name__ == "__main__":
    unittest.main()
