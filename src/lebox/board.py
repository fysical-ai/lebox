"""The board: a list of modules on one servo bus, read in real units."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .bus import ServoBus
from .calibration import Calibration, CalibrationError
from .modules import DEFAULT_MODULES, ModuleSpec


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

    def close(self) -> None:
        self.bus.close()

    def __enter__(self) -> "Board":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
