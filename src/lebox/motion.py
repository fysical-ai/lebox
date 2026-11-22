"""Driving a module: the only code that puts torque on a servo.

Every move follows the same rules, so nothing printed can be snapped and
nothing keeps pushing against an obstacle:

1. Refuse to start unless the servo's stored torque cap is at or below the
   allowed maximum. The cap is written once by ``lebox setup-motors``.
2. Set the working torque limit, read it back, and refuse to start if the
   servo did not take it.
3. Creep: the commanded position is never more than a few degrees ahead of
   where the servo actually is.
4. If the servo stops making progress it has stalled. Release at once.
5. Always end with torque off, whatever happened, including Ctrl+C.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .bus import ServoBus


class MotionError(Exception):
    """A move was refused or did not finish."""


class StallError(MotionError):
    """The servo stopped making progress before it reached the target."""

    def __init__(self, servo_id: int, position: int, target: int):
        super().__init__(
            f"servo {servo_id} stalled at {position}, {abs(target - position)} counts short of {target}; "
            "torque released. Something is blocking it, or the torque limit is too low to move it."
        )
        self.servo_id = servo_id
        self.position = position
        self.target = target


@dataclass(frozen=True)
class MotionSettings:
    torque: int = 150
    """Working torque limit for moves, per mille of stall torque."""
    max_cap: int = 400
    """Refuse to drive a servo whose stored cap is above this."""
    lead_ticks: int = 60
    """How far ahead of the present position the goal may be (60 counts is about 5 degrees)."""
    period_s: float = 0.02
    stall_s: float = 0.25
    """No progress for this long means stalled."""
    progress_ticks: int = 3
    tolerance_ticks: int = 6
    """Close enough to the target (6 counts is about half a degree)."""
    settle_s: float = 0.15
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if not 0 < self.torque <= self.max_cap <= 1000:
            raise MotionError(
                f"torque ({self.torque}) must be above 0 and at most the allowed cap ({self.max_cap}), in per mille"
            )
        if self.lead_ticks <= self.tolerance_ticks:
            raise MotionError("lead_ticks must be larger than tolerance_ticks")


class Clock:
    """Wall-clock time. The simulator supplies its own (see ``SimClock``)."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class SimClock(Clock):
    """Simulated time: sleeping advances the simulated bus instead of waiting."""

    def __init__(self, bus, on_tick: Callable[[float], None] | None = None):
        self._bus = bus
        self._on_tick = on_tick

    def now(self) -> float:
        return self._bus.time

    def sleep(self, seconds: float) -> None:
        self._bus.advance(seconds)
        if self._on_tick is not None:
            self._on_tick(self._bus.time)


def drive(
    bus: ServoBus,
    servo_id: int,
    target: int,
    settings: MotionSettings = MotionSettings(),
    clock: Clock | None = None,
) -> int:
    """Drive one servo to ``target`` (encoder counts), release it, and return where it ended.

    Raises ``MotionError`` if the move is refused and ``StallError`` if the
    servo stalls. Torque is off when this returns or raises.
    """
    clock = clock or Clock()

    cap = bus.read_torque_cap(servo_id)
    if cap > settings.max_cap:
        raise MotionError(
            f"servo {servo_id} has a stored torque cap of {cap / 10:g} %, above the allowed {settings.max_cap / 10:g} %. "
            "Run 'lebox setup-motors' to set the cap before driving it."
        )
    limit = min(settings.torque, cap)
    bus.write_torque_limit(servo_id, limit)
    stored = bus.read_torque_limit(servo_id)
    if stored != limit:
        raise MotionError(
            f"servo {servo_id} did not take the torque limit: wrote {limit}, read back {stored}. Not enabling torque."
        )

    try:
        position = bus.read_position(servo_id)
        bus.write_goal(servo_id, position)  # so that enabling torque does not jump
        bus.set_torque(servo_id, True)

        started = clock.now()
        progress_position, progress_time = position, started
        while abs(target - position) > settings.tolerance_ticks:
            lead = max(-settings.lead_ticks, min(settings.lead_ticks, target - position))
            bus.write_goal(servo_id, position + lead)
            clock.sleep(settings.period_s)
            position = bus.read_position(servo_id)
            now = clock.now()
            if abs(position - progress_position) >= settings.progress_ticks:
                progress_position, progress_time = position, now
            elif now - progress_time >= settings.stall_s:
                raise StallError(servo_id, position, target)
            if now - started > settings.timeout_s:
                raise MotionError(
                    f"servo {servo_id} did not reach {target} within {settings.timeout_s:g} s (at {position}); "
                    "torque released."
                )
        bus.write_goal(servo_id, target)
        clock.sleep(settings.settle_s)
    finally:
        bus.set_torque(servo_id, False)

    clock.sleep(settings.settle_s)
    return bus.read_position(servo_id)
