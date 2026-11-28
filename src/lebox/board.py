"""The board: a list of modules on one servo bus, read in real units."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .bus import ServoBus
from .calibration import Calibration, CalibrationError
from .modules import DEFAULT_MODULES, ModuleSpec
from .motion import Clock, MotionError, MotionSettings, drive


@dataclass(frozen=True)
class Reading:
    """One module's state at one moment."""

    name: str
    servo_id: int
    raw: int
    """Encoder counts, exactly as read."""
    value: float
    """Position on the module's scale."""
    unit: str
    label: str | None
    """The marking the element is at, or ``None`` between markings."""
    in_work_range: bool


@dataclass(frozen=True)
class MoveResult:
    """Where a driven module ended up after its torque was released."""

    name: str
    target: float
    reached: float
    error: float
    """Reached minus target, on the module's scale."""
    error_ticks: int
    seconds: float


class Board:
    def __init__(
        self,
        bus: ServoBus,
        calibration: Calibration,
        specs: Iterable[ModuleSpec] = DEFAULT_MODULES,
    ):
        self.bus = bus
        self.calibration = calibration
        self.specs = tuple(specs)

        names = [spec.name for spec in self.specs]
        ids = [spec.servo_id for spec in self.specs]
        if len(set(names)) != len(names):
            raise ValueError(f"module names must be unique, got {names}")
        if len(set(ids)) != len(ids):
            raise ValueError(f"servo ids must be unique, got {ids}")

        missing = [name for name in names if name not in calibration.modules]
        if missing:
            raise CalibrationError(
                f"calibration '{calibration.board_id}' has no entry for: {', '.join(missing)}. "
                "Run 'lebox calibrate' for this board."
            )
        self._by_name = {spec.name: spec for spec in self.specs}

    def spec(self, name: str) -> ModuleSpec:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no module named '{name}' (have: {', '.join(self._by_name)})") from None

    def read_module(self, name: str) -> Reading:
        spec = self.spec(name)
        cal = self.calibration.modules[name]
        raw = self.bus.read_position(spec.servo_id)
        value = spec.value_from_raw(raw, cal.raw_lo, cal.raw_hi)
        return Reading(
            name=name,
            servo_id=spec.servo_id,
            raw=raw,
            value=value,
            unit=spec.unit,
            label=spec.label_at(value),
            in_work_range=spec.in_work_range(value),
        )

    def read(self) -> dict[str, Reading]:
        """Every module's state, in board order."""
        return {spec.name: self.read_module(spec.name) for spec in self.specs}

    def values(self) -> dict[str, float]:
        """Just the scale values. This is the state that gets logged and scored."""
        return {name: reading.value for name, reading in self.read().items()}

    def raw_for(self, name: str, target: float | int | str) -> int:
        """Encoder position for a target such as ``3``, ``"II"`` or ``"closed"``."""
        spec = self.spec(name)
        cal = self.calibration.modules[name]
        return round(spec.raw_from_value(spec.resolve(target), cal.raw_lo, cal.raw_hi))

    def passive(self) -> None:
        """Torque off everywhere: the robot (or a hand) can move every module."""
        for spec in self.specs:
            self.bus.set_torque(spec.servo_id, False)

    def move(
        self,
        targets: Mapping[str, float | int | str],
        settings: MotionSettings = MotionSettings(),
        clock: Clock | None = None,
    ) -> dict[str, MoveResult]:
        """Drive modules to targets, one at a time, releasing each when it arrives.

        Every target is checked before anything moves. Whatever happens, all
        modules are passive again when this returns or raises.
        """
        clock = clock or Clock()
        plan = [(name, self.spec(name).resolve(target), self.raw_for(name, target)) for name, target in targets.items()]
        results: dict[str, MoveResult] = {}
        try:
            for name, value, raw in plan:
                spec = self.spec(name)
                cal = self.calibration.modules[name]
                started = clock.now()
                final = drive(self.bus, spec.servo_id, raw, settings, clock)
                reached = spec.value_from_raw(final, cal.raw_lo, cal.raw_hi)
                results[name] = MoveResult(
                    name=name,
                    target=value,
                    reached=reached,
                    error=reached - value,
                    error_ticks=final - raw,
                    seconds=clock.now() - started,
                )
        finally:
            self.passive()
        return results

    def reset(
        self,
        start: Mapping[str, float | int | str],
        settings: MotionSettings = MotionSettings(),
        clock: Clock | None = None,
    ) -> dict[str, MoveResult]:
        """Put modules in a start state and leave the board passive.

        Raises ``MotionError`` if a module ends further from its start than
        its marking tolerance, i.e. it would read as a different position.
        """
        results = self.move(start, settings, clock)
        for name, result in results.items():
            if abs(result.error) > self.spec(name).snap:
                raise MotionError(
                    f"{name} was sent to {result.target:g} but rests at {result.reached:.3g} after release; "
                    "the reset did not hold."
                )
        return results

    def close(self) -> None:
        self.bus.close()

    def __enter__(self) -> "Board":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
