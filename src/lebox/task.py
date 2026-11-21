"""Tasks: what the robot is asked to do, and what counts as done.

A task is a small YAML file::

    name: slider_to_3
    instruction: "Move the slider to 3"
    start:
      slider: {sample: [1, 2, 4, 5]}
      dial: 50
    goal:
      slider: {target: 3, tol: 0.15}
    others: unchanged
    hold_s: 1.0
    timeout_s: 30

``start`` says where modules are put before an episode. Per module it is a
value or marking (``50``, ``II``), ``{sample: [...]}`` to pick one of a list,
``{range: [low, high]}`` to pick anywhere in between, or ``random`` for the
whole working range. Modules left out stay where they are.

``goal`` says where modules must end up. Per module it is a value or marking,
``{target: 3, tol: 0.15}``, or a one- or two-sided ``{min: 75}`` /
``{max: 25}``. Several modules mean all of them at once.

``others`` is ``unchanged`` (the default: touching anything else is a side
effect) or ``ignore``. ``hold_s`` is how long the goal must hold.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .modules import DEFAULT_MODULES, ModuleError, ModuleSpec

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_TOP_KEYS = {"name", "instruction", "start", "goal", "others", "others_tol", "hold_s", "timeout_s"}
_BUILTIN_PACKAGE = "lebox.builtin_tasks"


class TaskError(ValueError):
    """The task file is wrong. The message says where."""


@dataclass(frozen=True)
class Goal:
    module: str
    low: float
    high: float
    target: float | None = None
    """The nominal value, when the goal was given as a target."""
    margin: float = 0.0
    """A start must be at least this far outside the goal."""

    def distance(self, value: float) -> float:
        """How far ``value`` is outside the goal interval; 0 inside it."""
        return max(self.low - value, 0.0, value - self.high)

    def satisfied(self, value: float) -> bool:
        return self.low <= value <= self.high


@dataclass(frozen=True)
class Start:
    kind: str
    """``fixed``, ``choice`` or ``range``."""
    values: tuple[float, ...]
    """One value, the options, or (low, high)."""


@dataclass(frozen=True)
class Task:
    name: str
    instruction: str
    goals: tuple[Goal, ...]
    start: Mapping[str, Start]
    others: str = "unchanged"
    others_tol: Mapping[str, float] = field(default_factory=dict)
    hold_s: float = 1.0
    timeout_s: float = 30.0
    source: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    """The task as written, for storing next to results."""

    @property
    def goal_modules(self) -> tuple[str, ...]:
        return tuple(goal.module for goal in self.goals)

    def sample_start(self, rng: random.Random | None = None) -> dict[str, float]:
        """One start state: a scale value for every module the task places.

        A start in a goal module is always at least the goal's margin away
        from the goal, so no episode begins already solved.
        """
        rng = rng or random.Random()
        goals = {goal.module: goal for goal in self.goals}
        state: dict[str, float] = {}
        for module, start in self.start.items():
            if start.kind == "fixed":
                state[module] = start.values[0]
            elif start.kind == "choice":
                state[module] = rng.choice(start.values)
            else:
                state[module] = _sample_range(start.values, goals.get(module), rng)
        return state

    # Loading

    @classmethod
    def from_dict(cls, data: Any, specs: Iterable[ModuleSpec] = DEFAULT_MODULES) -> "Task":
        if not isinstance(data, dict):
            raise TaskError("a task must be a mapping of settings (name, instruction, start, goal, ...)")
        by_name = {spec.name: spec for spec in specs}

        unknown = sorted(set(map(str, data)) - _TOP_KEYS)
        if unknown:
            raise TaskError(f"unknown setting(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(_TOP_KEYS))}")

        name = data.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise TaskError("'name' is required: lower-case letters, digits, '-' and '_'")
        instruction = data.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise TaskError(f"{name}: 'instruction' is required, the sentence given to the robot")

        goals = _parse_goals(name, data.get("goal"), by_name)
        start = _parse_start(name, data.get("start"), by_name, goals)

        others = data.get("others", "unchanged")
        if others not in ("unchanged", "ignore"):
            raise TaskError(f"{name}: 'others' must be 'unchanged' or 'ignore', got {others!r}")
        others_tol = _parse_others_tol(name, data.get("others_tol"), by_name, goals)

        hold_s = _number(name, "hold_s", data.get("hold_s", 1.0))
        timeout_s = _number(name, "timeout_s", data.get("timeout_s", 30.0))
        if hold_s < 0:
            raise TaskError(f"{name}: 'hold_s' cannot be negative")
        if timeout_s <= hold_s:
            raise TaskError(f"{name}: 'timeout_s' ({timeout_s:g}) must be longer than 'hold_s' ({hold_s:g})")

        return cls(
            name=name,
            instruction=instruction.strip(),
            goals=goals,
            start=start,
            others=others,
            others_tol=others_tol,
            hold_s=hold_s,
            timeout_s=timeout_s,
            source=dict(data),
        )

    @classmethod
    def from_yaml(cls, text: str, specs: Iterable[ModuleSpec] = DEFAULT_MODULES) -> "Task":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise TaskError(f"not valid YAML: {exc}") from exc
        return cls.from_dict(data, specs)

    @classmethod
    def load(cls, path: Path | str, specs: Iterable[ModuleSpec] = DEFAULT_MODULES) -> "Task":
        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise TaskError(f"cannot read task file {source}: {exc.strerror or exc}") from None
        try:
            return cls.from_yaml(text, specs)
        except TaskError as exc:
            raise TaskError(f"{source}: {exc}") from None


def builtin_names() -> list[str]:
    """Names of the tasks that ship with LeBox."""
    return sorted(
        entry.name[: -len(".yaml")]
        for entry in resources.files(_BUILTIN_PACKAGE).iterdir()
        if entry.name.endswith(".yaml")
    )


def load_builtin(name: str, specs: Iterable[ModuleSpec] = DEFAULT_MODULES) -> Task:
    if name not in builtin_names():
        raise TaskError(f"no built-in task named '{name}'. Run 'lebox task list' to see them.")
    text = resources.files(_BUILTIN_PACKAGE).joinpath(f"{name}.yaml").read_text(encoding="utf-8")
    try:
        return Task.from_yaml(text, specs)
    except TaskError as exc:
        raise TaskError(f"built-in task {name}: {exc}") from None


def find_task(name_or_path: str, specs: Iterable[ModuleSpec] = DEFAULT_MODULES) -> Task:
    """A built-in task by name, or a task file by path."""
    if name_or_path in builtin_names():
        return load_builtin(name_or_path, specs)
    if Path(name_or_path).is_file() or name_or_path.endswith((".yaml", ".yml")):
        return Task.load(name_or_path, specs)
    raise TaskError(
        f"'{name_or_path}' is neither a built-in task nor a task file. Run 'lebox task list' to see the built-in ones."
    )


# Parsing helpers


def _number(task: str, what: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskError(f"{task}: '{what}' must be a number, got {value!r}")
    return float(value)


def _spec(task: str, section: str, module: Any, by_name: Mapping[str, ModuleSpec]) -> ModuleSpec:
    if module not in by_name:
        raise TaskError(f"{task}: {section} names an unknown module {module!r} (have: {', '.join(by_name)})")
    return by_name[module]


def _resolve(task: str, section: str, spec: ModuleSpec, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TaskError(f"{task}: {section} {spec.name}: expected a number or a marking, got {value!r}")
    try:
        return spec.resolve(value)
    except ModuleError as exc:
        raise TaskError(f"{task}: {section} {exc}") from None


def _parse_goals(task: str, raw: Any, by_name: Mapping[str, ModuleSpec]) -> tuple[Goal, ...]:
    if not isinstance(raw, dict) or not raw:
        raise TaskError(f"{task}: 'goal' is required and must name at least one module")
    goals = []
    for module, entry in raw.items():
        spec = _spec(task, "goal", module, by_name)
        if not isinstance(entry, dict):
            target = _resolve(task, "goal", spec, entry)
            goals.append(Goal(spec.name, target - spec.snap, target + spec.snap, target, margin=spec.snap))
            continue
        keys = set(entry)
        if "target" in keys:
            if keys - {"target", "tol"}:
                raise TaskError(f"{task}: goal {module}: with 'target' only 'tol' may be added, got {sorted(keys)}")
            target = _resolve(task, "goal", spec, entry["target"])
            tol = _number(task, f"goal {module} tol", entry.get("tol", spec.snap))
            if tol <= 0:
                raise TaskError(f"{task}: goal {module}: 'tol' must be greater than 0")
            goals.append(Goal(spec.name, target - tol, target + tol, target, margin=tol))
        elif keys and keys <= {"min", "max"}:
            low = _number(task, f"goal {module} min", entry["min"]) if "min" in keys else spec.stops[0]
            high = _number(task, f"goal {module} max", entry["max"]) if "max" in keys else spec.stops[1]
            if high < spec.stops[0] or low > spec.stops[1]:
                given = " and ".join(f"{key} {entry[key]:g}" for key in ("min", "max") if key in keys)
                raise TaskError(
                    f"{task}: goal {module}: {given} is out of reach "
                    f"(the module travels {spec.stops[0]:g} to {spec.stops[1]:g})"
                )
            if not low < high:
                raise TaskError(f"{task}: goal {module}: 'min' ({low:g}) must be below 'max' ({high:g})")
            goals.append(Goal(spec.name, low, high, margin=spec.snap))
        else:
            raise TaskError(
                f"{task}: goal {module}: give a value, {{target, tol}} or {{min, max}}; got {sorted(map(str, keys))}"
            )
    return tuple(goals)


def _parse_start(
    task: str, raw: Any, by_name: Mapping[str, ModuleSpec], goals: tuple[Goal, ...]
) -> dict[str, Start]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TaskError(f"{task}: 'start' must map module names to start positions")
    goal_by_module = {goal.module: goal for goal in goals}
    start: dict[str, Start] = {}
    for module, entry in raw.items():
        spec = _spec(task, "start", module, by_name)
        goal = goal_by_module.get(spec.name)
        if isinstance(entry, str) and entry.strip().lower() == "random":
            parsed = Start("range", spec.work)
        elif isinstance(entry, dict):
            if set(entry) == {"sample"}:
                options = entry["sample"]
                if not isinstance(options, list) or not options:
                    raise TaskError(f"{task}: start {module}: 'sample' needs a list of positions")
                parsed = Start("choice", tuple(_resolve(task, "start", spec, option) for option in options))
            elif set(entry) == {"range"}:
                bounds = entry["range"]
                if not isinstance(bounds, list) or len(bounds) != 2:
                    raise TaskError(f"{task}: start {module}: 'range' needs [low, high]")
                low, high = (_resolve(task, "start", spec, bound) for bound in bounds)
                if not low < high:
                    raise TaskError(f"{task}: start {module}: range low ({low:g}) must be below high ({high:g})")
                parsed = Start("range", (low, high))
            else:
                raise TaskError(
                    f"{task}: start {module}: give a value, {{sample: [...]}}, {{range: [low, high]}} or 'random'"
                )
        else:
            parsed = Start("fixed", (_resolve(task, "start", spec, entry),))

        if goal is not None:
            margin = goal.margin
            if parsed.kind == "range":
                low, high = parsed.values
                if not (low < goal.low - margin or high > goal.high + margin):
                    raise TaskError(f"{task}: start {module}: the whole range lies in or next to the goal")
            else:
                for value in parsed.values:
                    if goal.distance(value) <= margin:
                        raise TaskError(
                            f"{task}: start {module}: {value:g} is in or next to the goal "
                            f"({goal.low:g} to {goal.high:g}); the episode would begin solved"
                        )
        start[spec.name] = parsed

    missing = [goal.module for goal in goals if goal.module not in start]
    if missing:
        raise TaskError(
            f"{task}: every goal module needs a start, otherwise an episode could begin solved; "
            f"missing: {', '.join(missing)}"
        )
    return start


def _parse_others_tol(
    task: str, raw: Any, by_name: Mapping[str, ModuleSpec], goals: tuple[Goal, ...]
) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TaskError(f"{task}: 'others_tol' must map module names to tolerances")
    goal_modules = {goal.module for goal in goals}
    result = {}
    for module, value in raw.items():
        spec = _spec(task, "others_tol", module, by_name)
        if spec.name in goal_modules:
            raise TaskError(f"{task}: others_tol {module}: this is a goal module, set its 'tol' under 'goal'")
        tol = _number(task, f"others_tol {module}", value)
        if tol <= 0:
            raise TaskError(f"{task}: others_tol {module}: must be greater than 0")
        result[spec.name] = tol
    return result


def _sample_range(bounds: tuple[float, ...], goal: Goal | None, rng: random.Random) -> float:
    low, high = bounds
    if goal is None:
        return rng.uniform(low, high)
    margin = goal.margin
    # The allowed part of the range is what lies outside the goal plus its margin.
    pieces = []
    if low < goal.low - margin:
        pieces.append((low, min(high, goal.low - margin)))
    if high > goal.high + margin:
        pieces.append((max(low, goal.high + margin), high))
    if not pieces:
        raise TaskError(f"start range {low:g} to {high:g} leaves no room outside the goal")
    lengths = [b - a for a, b in pieces]
    a, b = rng.choices(pieces, weights=lengths)[0]
    return rng.uniform(a, b)
