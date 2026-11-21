"""Traces: the board state over one episode.

A trace is a list of samples, each a time in seconds and the scale value of
every module. On disk it is JSON Lines, one sample per line::

    {"t": 0.0, "values": {"switch": 0.0, "dial": 50.0, "plunger": 2.0, "slider": 1.0}}

The trace is the raw record. Outcomes are computed from it, so an episode can
be scored again later under a different task definition.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


class TraceError(ValueError):
    """The trace file is malformed."""


@dataclass(frozen=True)
class Sample:
    t: float
    values: Mapping[str, float]


def save_trace(path: Path | str, samples: Iterable[Sample]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps({"t": sample.t, "values": dict(sample.values)}) + "\n")
    return target


def load_trace(path: Path | str) -> list[Sample]:
    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TraceError(f"cannot read trace file {source}: {exc.strerror or exc}") from None
    samples: list[Sample] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            t = entry["t"]
            values = entry["values"]
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not isinstance(values, dict):
                raise TypeError("'t' must be a number and 'values' a mapping")
            clean = {}
            for name, value in values.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise TypeError(f"value for '{name}' is not a finite number")
                clean[str(name)] = float(value)
            if not math.isfinite(t):
                raise TypeError("'t' is not a finite number")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise TraceError(f"{source}, line {number}: {exc}") from None
        samples.append(Sample(float(t), clean))
    if not samples:
        raise TraceError(f"{source} holds no samples")
    return samples
