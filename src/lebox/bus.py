"""The servo bus: the one place LeBox talks to hardware.

``ServoBus`` is the interface the rest of the code relies on. ``SimBus``
implements it in memory, with encoders, hard stops and simple motors, so
everything above it can be built and tested without a board. ``FeetechBus``
(in ``feetech.py``) implements the same interface on a real serial bus.

Torque values are per mille of the servo's stall torque: 1000 is 100 %.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol

from .calibration import Calibration
from .modules import DEFAULT_MODULES, TICKS_PER_REV, ModuleSpec


class BusError(Exception):
    """A servo did not answer or refused a command."""


@dataclass(frozen=True)
class ServoStatus:
    """A snapshot of one servo, for diagnostics."""

    servo_id: int
    model: int
    position: int
    voltage: float
    temperature: int
    torque_enabled: bool
    torque_limit: int
    """Working torque limit, per mille. Volatile: it resets at power-on."""
    torque_cap: int
    """Stored maximum torque, per mille. The servo loads the working limit from it at power-on."""


class ServoBus(Protocol):
    def ping(self, servo_id: int) -> bool:
        """Whether a servo with this id answers."""

    def read_position(self, servo_id: int) -> int:
        """Present encoder position in counts (0..4095)."""

    def set_torque(self, servo_id: int, enabled: bool) -> None:
        """Torque off makes the module a passive, back-drivable joint."""

    def write_goal(self, servo_id: int, raw: int) -> None:
        """Position the servo drives to while torque is on."""

    def read_torque_limit(self, servo_id: int) -> int: ...

    def write_torque_limit(self, servo_id: int, permille: int) -> None: ...

    def read_torque_cap(self, servo_id: int) -> int: ...

    def write_torque_cap(self, servo_id: int, permille: int) -> None:
        """Store the maximum torque in the servo's own memory."""

    def write_id(self, servo_id: int, new_id: int) -> None: ...

    def read_status(self, servo_id: int) -> ServoStatus: ...

    def close(self) -> None:
        """Release the port."""


@dataclass
class SimServo:
    position: float
    stop_a: float = 0.0
    stop_b: float = TICKS_PER_REV - 1.0
    torque: bool = False
    goal: float | None = None
    torque_limit: int = 1000
    torque_cap: int = 1000
    held: bool = False
    """Something blocks the element: a hand, a gripper, a jammed part."""
    min_moving_torque: int = 60
    """Below this torque limit the simulated motor cannot overcome friction."""
    speed: float = 3000.0
    """Counts per second when driving."""

    @property
    def limits(self) -> tuple[float, float]:
        return (min(self.stop_a, self.stop_b), max(self.stop_a, self.stop_b))


class SimBus:
    """An in-memory board: servos with encoders, hard stops and simple motors.

    Time only passes when ``advance`` is called, so tests run instantly and
    give the same result every time.
    """

    MODEL = 777

    def __init__(self, servos: Mapping[int, SimServo], noise_ticks: float = 0.0, seed: int | None = None):
        self._servos = dict(servos)
        self._noise = noise_ticks
        self._random = random.Random(seed)
        self.closed = False
        self.time = 0.0

    @classmethod
    def for_board(
        cls,
        calibration: Calibration,
        specs: Iterable[ModuleSpec] = DEFAULT_MODULES,
        start: Mapping[str, float] | None = None,
        **kwargs,
    ) -> "SimBus":
        """A simulated board whose stops sit where ``calibration`` says they are.

        ``start`` gives scale values per module name; modules left out start at
        the middle of their working range.
        """
        start = dict(start or {})
        servos = {}
        for spec in specs:
            cal = calibration.modules[spec.name]
            value = start.pop(spec.name, sum(spec.work) / 2.0)
            servos[spec.servo_id] = SimServo(
                position=spec.raw_from_value(value, cal.raw_lo, cal.raw_hi),
                stop_a=cal.raw_lo,
                stop_b=cal.raw_hi,
            )
        if start:
            raise KeyError(f"unknown module(s) in start: {', '.join(sorted(start))}")
        return cls(servos, **kwargs)

    def _servo(self, servo_id: int) -> SimServo:
        if self.closed:
            raise BusError("the bus is closed")
        try:
            return self._servos[servo_id]
        except KeyError:
            raise BusError(f"no servo with id {servo_id} answered") from None

    # The ServoBus interface

    def ping(self, servo_id: int) -> bool:
        if self.closed:
            raise BusError("the bus is closed")
        return servo_id in self._servos

    def read_position(self, servo_id: int) -> int:
        servo = self._servo(servo_id)
        noise = self._random.uniform(-self._noise, self._noise) if self._noise else 0.0
        return round(servo.position + noise)

    def set_torque(self, servo_id: int, enabled: bool) -> None:
        servo = self._servo(servo_id)
        servo.torque = bool(enabled)
        if enabled and servo.goal is None:
            servo.goal = servo.position

    def torque_enabled(self, servo_id: int) -> bool:
        return self._servo(servo_id).torque

    def write_goal(self, servo_id: int, raw: int) -> None:
        self._servo(servo_id).goal = float(raw)

    def read_torque_limit(self, servo_id: int) -> int:
        return self._servo(servo_id).torque_limit

    def write_torque_limit(self, servo_id: int, permille: int) -> None:
        self._servo(servo_id).torque_limit = _permille(permille)

    def read_torque_cap(self, servo_id: int) -> int:
        return self._servo(servo_id).torque_cap

    def write_torque_cap(self, servo_id: int, permille: int) -> None:
        self._servo(servo_id).torque_cap = _permille(permille)

    def write_id(self, servo_id: int, new_id: int) -> None:
        servo = self._servo(servo_id)
        if new_id == servo_id:
            return
        if new_id in self._servos:
            raise BusError(f"id {new_id} is already taken on this bus")
        del self._servos[servo_id]
        self._servos[new_id] = servo

    def read_status(self, servo_id: int) -> ServoStatus:
        servo = self._servo(servo_id)
        return ServoStatus(
            servo_id=servo_id,
            model=self.MODEL,
            position=self.read_position(servo_id),
            voltage=5.0,
            temperature=25,
            torque_enabled=servo.torque,
            torque_limit=servo.torque_limit,
            torque_cap=servo.torque_cap,
        )

    def close(self) -> None:
        self.closed = True

    # Simulated time and the outside world

    def advance(self, dt: float) -> None:
        """Let ``dt`` seconds pass: driven servos move toward their goals."""
        self.time += dt
        for servo in self._servos.values():
            if not servo.torque or servo.goal is None or servo.held:
                continue
            if servo.torque_limit < servo.min_moving_torque:
                continue
            low, high = servo.limits
            step = servo.speed * dt
            delta = servo.goal - servo.position
            servo.position = min(high, max(low, servo.position + max(-step, min(step, delta))))

    def power_cycle(self) -> None:
        """Switch the supply off and on: torque off, working limits reload from the stored caps."""
        for servo in self._servos.values():
            servo.torque = False
            servo.goal = None
            servo.torque_limit = servo.torque_cap

    def hold(self, servo_id: int, held: bool = True) -> None:
        """Block (or free) an element, as a hand holding the horn would."""
        self._servo(servo_id).held = held

    def push(self, servo_id: int, ticks: float) -> float:
        """Push the element by ``ticks``. Returns how far it actually moved.

        It stops at a hard stop, and it does not move at all while the servo
        holds it with torque on.
        """
        servo = self._servo(servo_id)
        if servo.torque:
            return 0.0
        low, high = servo.limits
        before = servo.position
        servo.position = min(high, max(low, before + ticks))
        return servo.position - before

    def place(self, servo_id: int, raw: float) -> None:
        """Put the element at an encoder position (clamped to the stops)."""
        servo = self._servo(servo_id)
        low, high = servo.limits
        servo.position = min(high, max(low, raw))


def _permille(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
        raise BusError(f"torque must be a whole number from 0 to 1000 per mille, got {value!r}")
    return value


def demo_positions(
    calibration: Calibration, t: float, specs: Iterable[ModuleSpec] = DEFAULT_MODULES
) -> dict[int, float]:
    """Encoder positions for a slow demo motion at time ``t`` seconds.

    Every module sweeps its working range at its own pace and sits mid-range at t = 0.
    """
    positions = {}
    for index, spec in enumerate(specs):
        cal = calibration.modules[spec.name]
        low, high = spec.work
        period = 6.0 + 2.5 * index
        value = (low + high) / 2.0 + (high - low) / 2.0 * math.sin(2.0 * math.pi * t / period)
        positions[spec.servo_id] = spec.raw_from_value(value, cal.raw_lo, cal.raw_hi)
    return positions
