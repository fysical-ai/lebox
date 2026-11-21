"""The servo bus: the one place LeBox talks to hardware.

``ServoBus`` is the small interface the rest of the code relies on. ``SimBus``
implements it in memory, with hard stops, so everything above it can be built
and tested without a board. The driver for the real Feetech bus implements the
same interface and is written against the hardware.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol

from .calibration import Calibration
from .modules import DEFAULT_MODULES, ModuleSpec


class BusError(Exception):
    """A servo did not answer or refused a command."""


class ServoBus(Protocol):
    def read_position(self, servo_id: int) -> int:
        """Present encoder position in counts (0..4095)."""

    def set_torque(self, servo_id: int, enabled: bool) -> None:
        """Torque off makes the module a passive, back-drivable joint."""

    def close(self) -> None:
        """Release the port."""


@dataclass
class SimServo:
    position: float
    stop_a: float
    stop_b: float
    torque: bool = False

    @property
    def limits(self) -> tuple[float, float]:
        return (min(self.stop_a, self.stop_b), max(self.stop_a, self.stop_b))


class SimBus:
    """An in-memory board: servos with encoders and hard stops, no motors yet."""

    def __init__(self, servos: Mapping[int, SimServo], noise_ticks: float = 0.0, seed: int | None = None):
        self._servos = dict(servos)
        self._noise = noise_ticks
        self._random = random.Random(seed)
        self.closed = False

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

    def read_position(self, servo_id: int) -> int:
        servo = self._servo(servo_id)
        noise = self._random.uniform(-self._noise, self._noise) if self._noise else 0.0
        return round(servo.position + noise)

    def set_torque(self, servo_id: int, enabled: bool) -> None:
        self._servo(servo_id).torque = bool(enabled)

    def torque_enabled(self, servo_id: int) -> bool:
        return self._servo(servo_id).torque

    def close(self) -> None:
        self.closed = True

    # What a hand or a gripper does to the board.

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
