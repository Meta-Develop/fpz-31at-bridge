"""Strict, bounded subset of GRBL blocks; reject every unimplemented word."""

import re
from dataclasses import replace
from math import isfinite

from .model import Command, GCodeError, ModalState

WORD = re.compile(r"([A-Z])([+-]?(?:\d+(?:\.\d*)?|\.\d+))")
GROUPS = {0: "motion", 1: "motion", 17: "plane", 20: "units", 21: "units",
          54: "wcs", 90: "distance", 91: "distance", 94: "feed-mode"}


def without_comments(line: str) -> str:
    output = []
    comment = False
    for char in line:
        if char == "(" and not comment:
            comment = True
        elif char == ")" and comment:
            comment = False
        elif char in "()":
            raise GCodeError(3, "Malformed comment")
        elif char == ";" and not comment:
            break
        elif not comment and not char.isspace():
            output.append(char.upper())
    if comment:
        raise GCodeError(3, "Unclosed comment")
    return "".join(output)


def parse_block(line: str, current: ModalState, *, jog: bool = False) -> Command:
    source = without_comments(line)
    words = []
    offset = 0
    while offset < len(source):
        match = WORD.match(source, offset)
        if not match:
            raise GCodeError(2, "Malformed G-code word")
        key, number = match.groups()
        value = float(number)
        if not isfinite(value):
            raise GCodeError(2, "Nonfinite G-code number")
        words.append((key, value))
        offset = match.end()
    seen = set()
    groups = set()
    changes = {}
    program_end = None
    for key, value in words:
        if key not in "GMXYZFSN":
            raise GCodeError(20, f"Unsupported word {key}")
        if key not in "GM" and key in seen:
            raise GCodeError(25, "Repeated word")
        seen.add(key)
        if key in "GMN" and not value.is_integer():
            raise GCodeError(23, "Command/line numbers must be integers")
        if key == "N" and not 0 <= value <= 10_000_000:
            raise GCodeError(27, "Invalid line number")
        if key == "G":
            code = int(value)
            if code not in GROUPS:
                raise GCodeError(20, f"G{code} is unavailable (including arcs and probing)")
            group = GROUPS[code]
            if group in groups:
                raise GCodeError(21, "Conflicting modal group")
            if jog and code not in (20, 21, 90, 91):
                raise GCodeError(16, "Jog accepts only units and distance G words")
            groups.add(group)
            if group in {"motion", "units", "distance"}:
                changes[group] = f"G{code}"
        if key == "M":
            if value not in (2, 3, 5, 30) or jog:
                raise GCodeError(20, "Unsupported M command")
            group = "program-flow" if value in (2, 30) else "spindle"
            if group in groups:
                raise GCodeError(21, "Conflicting M modal group")
            groups.add(group)
            if group == "program-flow":
                program_end = f"M{int(value)}"
            else:
                changes["spindle"] = f"M{int(value)}"
        if key in "FS" and value < 0:
            raise GCodeError(4, "Feed and spindle speed cannot be negative")
        if jog and key in "SN":
            raise GCodeError(16, "Invalid jog word")
    modes = replace(current, **changes)
    for key, value in words:
        if key == "F":
            feed = value * modes.scale
            if not isfinite(feed):
                raise GCodeError(2, "Feed conversion overflow")
            modes = replace(modes, feed_mm_min=feed)
        elif key == "S":
            modes = replace(modes, desired_rpm=value)
    axes = any(key in "XYZ" for key, _ in words)
    if jog:
        if not axes or "F" not in seen or modes.feed_mm_min <= 0:
            raise GCodeError(16, "Jog requires axes and positive explicit feed")
        modes = replace(modes, motion="G1")
    elif axes and modes.motion == "G1" and modes.feed_mm_min <= 0:
        raise GCodeError(22, "Linear motion requires a positive feed")
    return Command("jog" if jog else "gcode", source, tuple(words), modes, program_end)
