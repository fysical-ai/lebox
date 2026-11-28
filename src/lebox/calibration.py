"""Calibration data: the encoder reading at each module's two hard stops.

This file only holds, saves, loads and checks the numbers. The routine that
finds the stops on real hardware (``lebox calibrate``) comes with the servo
driver.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .modules import DEFAULT_MODULES, TICKS_PER_REV, ModuleSpec

FORMAT_VERSION = 1
BENCH_MARGIN_TICKS = 40
"""Room kept between a bench calibration's ends and the encoder's wrap point."""
_BOARD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class CalibrationError(Exception):
    """The calibration is missing, unreadable or does not fit the board."""


@dataclass(frozen=True)
class ModuleCalibration:
    raw_lo: int
    """Encoder reading at the low end of the scale (I, CLOSED, 0, 1)."""
    raw_hi: int
    """Encoder reading at the high end of the scale (II, OPEN, 4, 5)."""

    @property
    def travel_ticks(self) -> int:
        return abs(self.raw_hi - self.raw_lo)


@dataclass
class Calibration:
    board_id: str
    modules: dict[str, ModuleCalibration]
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    nominal: bool = False
    """True when the numbers come from the design, not from a real board."""
    bench: bool = False
    """True for a bench calibration: loose servos with no hard stops, ends set from the design travel."""

    @classmethod
    def from_design(cls, specs: Iterable[ModuleSpec] = DEFAULT_MODULES, board_id: str = "sim") -> "Calibration":
        """Design-value calibration with every module centred on the encoder.

        For the simulated board and for dry runs. Never use it to drive real servos.
        """
        modules = {}
        for spec in specs:
            half = spec.expected_travel_ticks / 2.0
            centre = TICKS_PER_REV / 2
            modules[spec.name] = ModuleCalibration(round(centre - half), round(centre + half))
        return cls(board_id=board_id, modules=modules, nominal=True)

    @classmethod
    def from_bench(
        cls,
        positions: Mapping[str, int],
        specs: Iterable[ModuleSpec] = DEFAULT_MODULES,
        board_id: str = "bench",
    ) -> "Calibration":
        """Calibration for loose servos on the bench, which have no hard stops.

        Wherever each horn sits now becomes the middle of its travel, and the
        design travel puts the two ends either side of it.
        """
        modules = {}
        for spec in specs:
            if spec.name not in positions:
                raise CalibrationError(f"{spec.name}: no position given for bench calibration")
            middle = positions[spec.name]
            half = spec.expected_travel_ticks / 2.0
            lowest, highest = BENCH_MARGIN_TICKS + half, TICKS_PER_REV - 1 - BENCH_MARGIN_TICKS - half
            if not lowest <= middle <= highest:
                raise CalibrationError(
                    f"{spec.name}: the horn is at {middle}, but it must sit between {lowest:.0f} and {highest:.0f} "
                    f"so that its whole travel fits in one turn of the encoder. "
                    f"Turn it towards {TICKS_PER_REV // 2} and calibrate again."
                )
            modules[spec.name] = ModuleCalibration(round(middle - half), round(middle + half))
        return cls(board_id=board_id, modules=modules, bench=True)

    def problems(self, specs: Iterable[ModuleSpec] = DEFAULT_MODULES, tolerance: float = 0.05) -> list[str]:
        """Reasons not to trust this calibration. An empty list means it looks sound.

        Measured travel more than ``tolerance`` away from the design travel points
        to a false stop (a tight print, something in the way) or a missed one.
        """
        found: list[str] = []
        for spec in specs:
            cal = self.modules.get(spec.name)
            if cal is None:
                found.append(f"{spec.name}: not calibrated")
                continue
            for end, raw in (("low", cal.raw_lo), ("high", cal.raw_hi)):
                if not 0 <= raw < TICKS_PER_REV:
                    found.append(f"{spec.name}: {end} stop reading {raw} is outside 0..{TICKS_PER_REV - 1}")
            expected = spec.expected_travel_ticks
            deviation = (cal.travel_ticks - expected) / expected
            if abs(deviation) > tolerance:
                found.append(
                    f"{spec.name}: travel is {cal.travel_ticks} counts, design says {expected:.0f} "
                    f"({deviation:+.0%}, limit {tolerance:.0%})"
                )
        return found

    def to_dict(self) -> dict:
        return {
            "format": FORMAT_VERSION,
            "board_id": self.board_id,
            "created": self.created,
            "nominal": self.nominal,
            "bench": self.bench,
            "modules": {
                name: {"raw_lo": cal.raw_lo, "raw_hi": cal.raw_hi} for name, cal in self.modules.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Calibration":
        try:
            if data["format"] != FORMAT_VERSION:
                raise CalibrationError(
                    f"calibration file format {data['format']} is not supported (expected {FORMAT_VERSION})"
                )
            modules = {
                name: ModuleCalibration(int(entry["raw_lo"]), int(entry["raw_hi"]))
                for name, entry in data["modules"].items()
            }
            return cls(
                board_id=str(data["board_id"]),
                modules=modules,
                created=str(data.get("created", "")),
                nominal=bool(data.get("nominal", False)),
                bench=bool(data.get("bench", False)),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise CalibrationError(f"calibration data is malformed: {exc!r}") from exc

    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path) if path is not None else default_path(self.board_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path | str) -> "Calibration":
        source = Path(path)
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CalibrationError(f"no calibration file at {source}") from None
        except json.JSONDecodeError as exc:
            raise CalibrationError(f"{source} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise CalibrationError(f"{source} does not hold a calibration")
        return cls.from_dict(data)


def lebox_home() -> Path:
    """Where LeBox keeps its files: ``$LEBOX_HOME`` or ``~/.lebox``."""
    return Path(os.environ.get("LEBOX_HOME") or Path.home() / ".lebox")


def default_path(board_id: str) -> Path:
    if not _BOARD_ID.fullmatch(board_id):
        raise CalibrationError(
            f"board id '{board_id}' may only contain letters, digits, '-' and '_' (it becomes a file name)"
        )
    return lebox_home() / "calibration" / f"{board_id}.json"
