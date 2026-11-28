"""Module descriptors: what each LeBox module is and how its scale works.

A module is one manipulable element (switch, dial, plunger, slider) coupled to
one bus servo. Software treats the board as a list of these descriptors, so a
different board is a different list.

Every module has a *scale*: the numbers a person reads off the tile.

    switch   degrees, -40 (I) .. 0 (O) .. +40 (II)
    dial     percent open, 0 (CLOSED) .. 100 (OPEN)
    plunger  ring number, 0 (pushed in) .. 4 (pulled out)
    slider   mark number, 1 .. 5

The scale is linear between the two hard stops. ``stops`` gives the scale
value at each stop; ``work`` is the slightly smaller range the software
commands and scores in, so nothing is ever driven into a stop on purpose.

Geometry comes from the LeBox design spec v0.2.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TICKS_PER_REV = 4096
"""Encoder counts per servo revolution (STS3215, 12-bit)."""


class ModuleError(ValueError):
    """A target or label that does not fit the module."""


@dataclass(frozen=True)
class Marking:
    """A position printed on the tile, e.g. ``II`` or ``CLOSED``."""

    label: str
    value: float


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    servo_id: int
    unit: str
    """Shown after the value: ``deg``, ``%`` or empty for a plain scale number."""
    stops: tuple[float, float]
    """Scale value at the low and the high hard stop."""
    work: tuple[float, float]
    """Scale range the software commands and scores in."""
    markings: tuple[Marking, ...]
    servo_travel_deg: float
    """Servo rotation between the two hard stops, from the design."""
    snap: float
    """How close to a marking (in scale units) still reads as 'at' it."""
    description: str = ""
    noun: str = ""
    """What people call the element, e.g. ``valve`` for the dial. Defaults to the module name."""
    colour: str = ""
    """The colour of the part the robot handles, used in generated instructions."""

    def __post_init__(self) -> None:
        lo, hi = self.stops
        if not lo < hi:
            raise ValueError(f"{self.name}: stops must be (low, high), got {self.stops}")
        if not (lo <= self.work[0] < self.work[1] <= hi):
            raise ValueError(f"{self.name}: work range {self.work} must lie inside stops {self.stops}")
        if not self.markings:
            raise ValueError(f"{self.name}: needs at least one marking")

    @property
    def phrase(self) -> str:
        """How the element is named in a sentence: ``the blue valve``."""
        return " ".join(part for part in ("the", self.colour, self.noun or self.name) if part)

    def phrase_value(self, value: float) -> str:
        """A scale value as it would be said: ``II``, ``CLOSED``, ``50%``, ``3``."""
        for marking in self.markings:
            # A marking on a hard stop resolves to the end of the work range; say the marking.
            clamped = min(self.work[1], max(self.work[0], marking.value))
            if math.isclose(clamped, value, abs_tol=1e-9):
                if not _is_number(marking.label):
                    return marking.label
                value = marking.value
                break
        if self.unit == "%":
            return f"{value:g}%"
        if self.unit == "deg":
            return f"{value:g} degrees"
        return f"{value:g}"

    @property
    def expected_travel_ticks(self) -> float:
        """Encoder counts between the two stops, if the build matches the design."""
        return self.servo_travel_deg * TICKS_PER_REV / 360.0

    def value_from_raw(self, raw: float, raw_lo: float, raw_hi: float) -> float:
        """Scale value for an encoder reading, given the readings at the two stops."""
        if raw_hi == raw_lo:
            raise ModuleError(f"{self.name}: calibration has no travel (both stops at {raw_lo})")
        lo, hi = self.stops
        return lo + (raw - raw_lo) / (raw_hi - raw_lo) * (hi - lo)

    def raw_from_value(self, value: float, raw_lo: float, raw_hi: float) -> float:
        """Encoder reading for a scale value. The inverse of :meth:`value_from_raw`."""
        lo, hi = self.stops
        return raw_lo + (value - lo) / (hi - lo) * (raw_hi - raw_lo)

    def nearest_marking(self, value: float) -> Marking:
        return min(self.markings, key=lambda m: abs(m.value - value))

    def label_at(self, value: float) -> str | None:
        """The marking the element is at, or ``None`` if it is between markings."""
        marking = self.nearest_marking(value)
        return marking.label if abs(marking.value - value) <= self.snap else None

    def in_work_range(self, value: float) -> bool:
        return self.work[0] <= value <= self.work[1]

    def resolve(self, target: float | int | str) -> float:
        """Turn a target such as ``3``, ``"2.5"``, ``"II"`` or ``"closed"`` into a scale value.

        Labels are matched without regard to case. Numbers must lie in the work
        range, with one exception: a marking that sits on a hard stop (the
        plunger's rings 0 and 4) resolves to the nearest end of the work range,
        so "plunger to 0" means fully in without driving into the stop.
        """
        value: float | None = None
        if isinstance(target, str):
            text = target.strip()
            for marking in self.markings:
                if marking.label.lower() == text.lower():
                    value = marking.value
                    break
            if value is None:
                try:
                    value = float(text)
                except ValueError:
                    labels = ", ".join(m.label for m in self.markings)
                    raise ModuleError(
                        f"{self.name}: '{target}' is neither a number nor one of the markings ({labels})"
                    ) from None
        else:
            value = float(target)
        if any(math.isclose(value, m.value, abs_tol=1e-9) for m in self.markings):
            value = min(self.work[1], max(self.work[0], value))
        if not math.isfinite(value) or not self.in_work_range(value):
            raise ModuleError(
                f"{self.name}: {target} is outside the working range {self.work[0]:g} to {self.work[1]:g}"
            )
        return value


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _arc_deg(travel_mm: float, gear_radius_mm: float) -> float:
    """Servo rotation that moves a rack ``travel_mm`` on a sector gear of the given radius."""
    return math.degrees(travel_mm / gear_radius_mm)


def _numbered(first: int, last: int) -> tuple[Marking, ...]:
    return tuple(Marking(str(n), float(n)) for n in range(first, last + 1))


# The dial is driven over 270 degrees (0..100 %) and its stop lug sits 2 degrees
# past each end, so the stops are a little outside 0 and 100 on the scale.
_DIAL_OVERTRAVEL = 2.0 / 270.0 * 100.0

SWITCH = ModuleSpec(
    name="switch",
    servo_id=1,
    unit="deg",
    stops=(-42.0, 42.0),
    work=(-40.0, 40.0),
    markings=(Marking("I", -40.0), Marking("O", 0.0), Marking("II", 40.0)),
    servo_travel_deg=84.0,
    snap=5.0,
    description="Knife switch, direct drive. I at the front, O in the middle, II at the rear.",
    noun="switch",
    colour="black",
)

DIAL = ModuleSpec(
    name="dial",
    servo_id=2,
    unit="%",
    stops=(-_DIAL_OVERTRAVEL, 100.0 + _DIAL_OVERTRAVEL),
    work=(0.0, 100.0),
    markings=(
        Marking("CLOSED", 0.0),
        Marking("25", 25.0),
        Marking("50", 50.0),
        Marking("75", 75.0),
        Marking("OPEN", 100.0),
    ),
    servo_travel_deg=274.0,
    snap=3.0,
    description="Valve handle, direct drive. Clockwise closes; 0 % is the clockwise end.",
    noun="valve",
    colour="blue",
)

PLUNGER = ModuleSpec(
    name="plunger",
    servo_id=3,
    unit="",
    stops=(0.0, 4.0),
    # Assumption: ring 0 shows when fully pushed in and ring 4 when fully pulled
    # out, 5 mm apart over the 20 mm travel. The work range stays 0.5 mm inside
    # each stop. To confirm on the printed rod.
    work=(0.1, 3.9),
    markings=_numbered(0, 4),
    servo_travel_deg=_arc_deg(20.0, 34.5),
    snap=0.15,
    description="Push-pull plunger on a sector gear (34.5 mm). 0 is pushed in, 4 is pulled out.",
    noun="plunger",
    colour="red",
)

SLIDER = ModuleSpec(
    name="slider",
    servo_id=4,
    unit="",
    # Marks 1..5 are 10 mm apart (+-20 mm); the slot ends are at +-20.3 mm.
    stops=(1.0 - 0.03, 5.0 + 0.03),
    work=(1.0, 5.0),
    markings=_numbered(1, 5),
    servo_travel_deg=_arc_deg(40.6, 36.0),
    snap=0.15,
    description="Slider on a sector gear (36 mm). Marks 1 to 5, 10 mm apart.",
    noun="slider",
    colour="white",
)

DEFAULT_MODULES: tuple[ModuleSpec, ...] = (SWITCH, DIAL, PLUNGER, SLIDER)
"""The standard 2 x 2 LeBox v0.2 board."""
