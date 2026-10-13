"""Pure XY count/feed plans, not validated physical G0/G1 trajectories.

Static conversion divides by axis scale, adds/subtracts 0.5, then truncates;
GUI captures corroborate integer-count moves and integer mm/sec S truncation.
This module implements that mathematical rounding rule with exact rationals,
not bit-for-bit emulation of the original application's floating-point edge
cases. Floats are interpreted by their decimal str(); use Fraction/Decimal for
explicit precision. Inches convert by exactly 127/5. No profile is implicit.

Only a permutation and sign mapping of orthogonal XY axes is supported. No
rotation, backlash, homing, Z, spindle, C barrier, transport, or completion is
provided. Frames describe candidate controller setpoints; G0/G1 physical path,
acceleration and coordinated interpolation still require independent proof.
"""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import math

from .fpz_protocol import absolute_xy_counts, feed_controller_units, relative_xy_counts
from .model import Command, Unsupported

Number = int | float | Decimal | Fraction
CountXY = tuple[int, int]
INT_MIN, INT_MAX = -(2**31), 2**31 - 1


class PlanningError(ValueError):
    """Invalid inputs or bounds; no plan/frames have been returned."""


def _number(value: Number) -> Fraction:
    if type(value) is bool or not isinstance(value, (int, float, Decimal, Fraction)):
        raise PlanningError("Finite numeric input required")
    if isinstance(value, float) and not math.isfinite(value):
        raise PlanningError("Nonfinite input")
    if isinstance(value, Decimal) and not value.is_finite():
        raise PlanningError("Nonfinite input")
    return Fraction(str(value)) if isinstance(value, float) else Fraction(value)


def _count(value: int) -> int:
    if type(value) is not int or not INT_MIN <= value <= INT_MAX:
        raise PlanningError("Signed 32-bit controller count required")
    return value


def _pair(value, label):
    if type(value) is not tuple or len(value) != 2:
        raise PlanningError(f"{label} must be an immutable pair")
    return value


def round_count(value: Number) -> int:
    """Nearest integer, exact ties away from zero; reject signed32 overflow."""
    value = _number(value)
    magnitude = abs(value) + Fraction(1, 2)
    rounded = magnitude.numerator // magnitude.denominator
    return _count(-rounded if value < 0 else rounded)


@dataclass(frozen=True)
class AxisMapping:
    """One logical axis: controller index0/1, direction +/-1, positive mm/count."""
    controller_axis: int
    direction: int
    mm_per_count: Number


@dataclass(frozen=True)
class XYProfile:
    """Required calibration and bounds, all explicitly supplied by the caller.

    axes is in logical (X,Y) order. origin_counts and travel_limits_counts are
    in controller (0,1) order. origin_counts is the controller count of work
    coordinate zero, not a request to set a device origin. Travel limits bound
    both requested and rounded endpoints; they do not prove intermediate path
    safety. Feed bounds cover requested AND quantized speed without clamping.
    """
    axes: tuple[AxisMapping, AxisMapping]
    origin_counts: CountXY
    travel_limits_counts: tuple[tuple[int, int], tuple[int, int]]
    min_feed_mm_s: Number
    max_feed_mm_s: Number
    rapid_mm_s: Number


@dataclass(frozen=True)
class XYPlan:
    """Candidate setpoints, never a report of a measured final position.

    effective_work_mm is the mathematical position implied by target_counts
    and the supplied profile. Requested/effective feeds remain distinct; the
    caller must decide whether quantization is acceptable before any execution.
    """
    frames: tuple[bytes, bytes]
    start_counts: CountXY
    target_counts: CountXY
    delta_counts: CountXY
    requested_work_mm: tuple[Fraction, Fraction]
    effective_work_mm: tuple[Fraction, Fraction]
    requested_feed_mm_s: Fraction
    controller_feed: int
    effective_feed_mm_min: int
    motion: str
    distance: str

    @property
    def requested_feed_mm_min(self) -> Fraction:
        return self.requested_feed_mm_s * 60

    @property
    def effective_feed_mm_s(self) -> int:
        return self.controller_feed


def _profile(profile: XYProfile):
    if not isinstance(profile, XYProfile):
        raise PlanningError("Explicit XYProfile required")
    axes = _pair(profile.axes, "axes")
    if not all(isinstance(axis, AxisMapping) for axis in axes):
        raise PlanningError("Two AxisMapping entries required")
    for axis in axes:
        if type(axis.controller_axis) is not int or axis.controller_axis not in (0, 1):
            raise PlanningError("Controller axis must be 0 or 1")
        if type(axis.direction) is not int or axis.direction not in (-1, 1):
            raise PlanningError("Axis direction must be +1 or -1")
    if axes[0].controller_axis == axes[1].controller_axis:
        raise PlanningError("XY mapping must be a permutation")
    scales = tuple(_number(axis.mm_per_count) for axis in axes)
    if any(scale <= 0 for scale in scales):
        raise PlanningError("Positive axis scales required")
    for value in _pair(profile.origin_counts, "origin_counts"):
        _count(value)
    limits = _pair(profile.travel_limits_counts, "travel_limits_counts")
    for bounds in limits:
        lo, hi = _pair(bounds, "axis limits")
        if _count(lo) > _count(hi):
            raise PlanningError("Reversed travel limits")
    low, high, rapid = map(_number, (profile.min_feed_mm_s, profile.max_feed_mm_s, profile.rapid_mm_s))
    if not 0 < low <= high <= INT_MAX:
        raise PlanningError("Positive ordered signed32 feed bounds required")
    _feed(rapid, low, high)  # Reject invalid rapid configuration even for G1.
    return scales, low, high, rapid


def _feed(speed: Fraction, low: Fraction, high: Fraction) -> int:
    if not low <= speed <= high:
        raise PlanningError("Requested speed outside configured bounds")
    integer = int(speed)  # Positive mm/sec: original integer branch truncates.
    if integer == 0:
        raise PlanningError("Requested speed is below integer S resolution")
    if not low <= integer <= high:
        raise PlanningError("Quantized speed outside configured bounds")
    return _count(integer)


def _words(command: Command):
    if not isinstance(command, Command) or command.kind not in ("gcode", "jog"):
        raise Unsupported("Only validated G-code/jog Command inputs are supported")
    modes = command.modes
    if (command.program_end is not None or modes.spindle != "M5"
            or modes.motion not in ("G0", "G1") or modes.units not in ("G20", "G21")
            or modes.distance not in ("G90", "G91")):
        raise Unsupported("XY planner cannot handle these modal/program semantics")
    if _number(modes.feed_mm_min) < 0 or _number(modes.desired_rpm) < 0:
        raise PlanningError("Negative modal feed/spindle value")
    words, groups = {}, set()
    group_of = {0: "motion", 1: "motion", 17: "plane", 20: "units", 21: "units",
                54: "wcs", 90: "distance", 91: "distance", 94: "feed-mode"}
    for key, raw in command.words:
        if key not in ("G", "X", "Y", "F", "N"):
            raise Unsupported("Z, spindle, arcs and other unhandled words cannot be dropped")
        value = _number(raw)
        if key == "G":
            if value.denominator != 1 or int(value) not in group_of:
                raise Unsupported("Unsupported G-code semantics")
            group = group_of[int(value)]
            if group in groups:
                raise PlanningError("Repeated modal group")
            groups.add(group)
            if group in ("motion", "units", "distance") and getattr(modes, group) != f"G{int(value)}":
                raise PlanningError("Command words disagree with resolved modes")
        else:
            if key in words:
                raise PlanningError("Repeated word")
            if key == "N" and (value.denominator != 1 or not 0 <= value <= 10_000_000):
                raise PlanningError("Invalid line number")
            if key == "F" and value <= 0:
                raise PlanningError("Explicit feed must be positive")
            words[key] = value
    if not ("X" in words or "Y" in words):
        raise Unsupported("XY motion words required; no feed-only or modal-only plans")
    return words


def plan_xy(command: Command, *, measured_counts: CountXY, profile: XYProfile) -> XYPlan:
    """Validate everything before encoding S plus M (G91) or MA (G90).

    No state is retained or advanced. Omitted axes keep measured counts exactly.
    G91 rounds each requested delta before adding it to measured position;
    G90 rounds the offset from the explicit work-zero origin. Explicit F words
    convert from block units/min exactly; retained modes.feed_mm_min is already
    canonical mm/min and must NOT receive another G20 scale factor.
    """
    scales, low, high, rapid = _profile(profile)
    words = _words(command)
    start = tuple(_count(v) for v in _pair(measured_counts, "measured_counts"))
    for value, (lo, hi) in zip(start, profile.travel_limits_counts):
        if not lo <= value <= hi:
            raise PlanningError("Measured starting position outside configured travel")
    unit = Fraction(127, 5) if command.modes.units == "G20" else Fraction(1)
    target, requested, effective = list(start), [], []
    for key, axis, scale in zip(("X", "Y"), profile.axes, scales):
        index, sign = axis.controller_axis, axis.direction
        origin = profile.origin_counts[index]
        current_mm = sign * (start[index] - origin) * scale
        requested_mm = current_mm
        if key in words:
            value_mm = words[key] * unit
            offset_counts = sign * value_mm / scale
            base = start[index] if command.modes.distance == "G91" else origin
            requested_mm = current_mm + value_mm if command.modes.distance == "G91" else value_mm
            unrounded_target = base + offset_counts
            lo, hi = profile.travel_limits_counts[index]
            if not lo <= unrounded_target <= hi:
                raise PlanningError("Requested endpoint outside configured travel")
            target[index] = _count(base + round_count(offset_counts))
            if not lo <= target[index] <= hi:
                raise PlanningError("Rounded endpoint outside configured travel")
        requested.append(requested_mm)
        effective.append(sign * (target[index] - origin) * scale)
    delta = tuple(_count(end - begin) for begin, end in zip(start, target))
    feed_mm_min = words["F"] * unit if "F" in words else _number(command.modes.feed_mm_min)
    speed = rapid if command.modes.motion == "G0" else feed_mm_min / 60
    feed = _feed(speed, low, high)
    # No encoder is invoked until every coordinate, mode and speed is valid.
    movement = (relative_xy_counts(*delta) if command.modes.distance == "G91"
                else absolute_xy_counts(*target))
    frames = (feed_controller_units(feed), movement)
    return XYPlan(frames, start, tuple(target), delta, tuple(requested), tuple(effective),
                  speed, feed, feed * 60, command.modes.motion, command.modes.distance)
