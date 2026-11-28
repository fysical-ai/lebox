"""Tasks: what the robot is asked to do, and what counts as done.

A task is an ordered list of steps over the board's state. Each step is one
of two kinds:

    reach   bring modules into a goal region (a target and a tolerance)
    hold    leave everything as it is for a set time ("wait 30 seconds")

One step makes a short task; many make a long-horizon one. The robot always
gets the whole instruction up front, so in a hold step it is the robot that
has to keep time.

A one-step task can be written in the short form::

    name: slider_to_3
    instruction: "Move the slider to 3"
    start:
      slider: {sample: [1, 2, 4, 5]}
    goal:
      slider: {target: 3, tol: 0.15}

and a longer one as steps::

    name: valve_wait_slider
    start:
      dial: {range: [0, 30]}
      slider: {sample: [1, 2]}
    steps:
      - say: ["Turn the blue valve to 50%", "Set the blue valve half open"]
        reach: {dial: 50}
        within_s: 20
      - say: "Wait 30 seconds"
        hold_s: 30
        early_s: 5
      - say: "Slide the slider to 5"
        reach: {slider: 5}
        within_s: 20

``start`` says where modules are put before an episode. Per module it is a
value or marking (``50``, ``II``), ``{sample: [...]}`` to pick one of a list,
``{range: [low, high]}`` to pick anywhere in between, or ``random`` for the
whole working range. Modules left out stay where they are.

A goal, per module, is a value or marking, ``{target: 3, tol: 0.15}``, a one-
or two-sided ``{min: 75}`` / ``{max: 25}``, or the word ``start``: wherever
that module was when the episode began ("put it back").

Times are relative. ``within_s`` is how long a reach step may take, counted
from the moment the step before it was completed. ``hold_s`` is how long a
hold lasts and ``early_s`` how much early the robot may move on.

The steps are the definition of the task. ``say`` is only the wording: give
several and one is picked per episode. With no wording at all, a plain
sentence is generated from the step itself.

``others`` is ``unchanged`` (the default: touching a module no step has
named is a side effect) or ``ignore``. ``hold_s`` at the top level is how
long the final goal must hold; ``settle_s`` is the same for earlier steps.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .modules import DEFAULT_MODULES, ModuleError, ModuleSpec

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_TOP_KEYS = {
    "name", "instruction", "start", "goal", "steps", "others", "others_tol", "hold_s", "settle_s", "timeout_s",
}
_STEP_KEYS = {"say", "reach", "within_s", "hold_s", "early_s"}
_BUILTIN_PACKAGE = "lebox.builtin_tasks"
DEFAULT_STEP_BUDGET_S = 30.0


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
    from_start: bool = False
    """The goal is wherever the module was when the episode began; ``low`` and
    ``high`` are then offsets either side of that position."""

    def resolved(self, start_value: float) -> "Goal":
        """This goal as a fixed region, given where the module started."""
        if not self.from_start:
            return self
        return replace(
            self, low=start_value + self.low, high=start_value + self.high, target=start_value, from_start=False
        )

    def distance(self, value: float) -> float:
        """How far ``value`` is outside the goal interval; 0 inside it."""
        self._require_fixed()
        return max(self.low - value, 0.0, value - self.high)

    def satisfied(self, value: float) -> bool:
        self._require_fixed()
        return self.low <= value <= self.high

    def _require_fixed(self) -> None:
        if self.from_start:
            raise TaskError(f"goal on {self.module} is relative to the start; resolve it with the start state first")

    def signature(self) -> str:
        if self.from_start:
            return f"{self.module}=start±{self.high:g}"
        if self.target is not None:
            return f"{self.module}={self.target:g}±{(self.high - self.low) / 2:g}"
        return f"{self.module}={self.low:g}..{self.high:g}"


@dataclass(frozen=True)
class Start:
    kind: str
    """``fixed``, ``choice`` or ``range``."""
    values: tuple[float, ...]
    """One value, the options, or (low, high)."""


@dataclass(frozen=True)
class Step:
    kind: str
    """``reach`` or ``hold``."""
    goals: tuple[Goal, ...] = ()
    say: tuple[str, ...] = ()
    """Wordings of this step; empty means generate one."""
    within_s: float | None = None
    """Reach: time allowed, from the completion of the step before."""
    hold_s: float | None = None
    """Hold: how long nothing may change."""
    early_s: float = 0.0
    """Hold: how much early the robot may move on."""

    def signature(self) -> str:
        if self.kind == "hold":
            early = f" (-{self.early_s:g}s)" if self.early_s else ""
            return f"hold {self.hold_s:g}s{early}"
        text = " & ".join(goal.signature() for goal in self.goals)
        return text + (f" within {self.within_s:g}s" if self.within_s is not None else "")


@dataclass(frozen=True)
class Task:
    name: str
    steps: tuple[Step, ...]
    start: Mapping[str, Start]
    instructions: tuple[str, ...] = ()
    """Wordings of the whole task; empty means compose one from the steps."""
    others: str = "unchanged"
    others_tol: Mapping[str, float] = field(default_factory=dict)
    hold_s: float = 1.0
    """How long the final goal must hold."""
    settle_s: float = 0.5
    """How long the goal of an earlier step must hold before the next step begins."""
    timeout_s: float = 30.0
    source: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    """The task as written, for storing next to results."""
    specs: Mapping[str, ModuleSpec] = field(default_factory=dict, compare=False, repr=False)

    @property
    def goals(self) -> tuple[Goal, ...]:
        """The goals of the first reach step: all of them, for a one-step task."""
        for step in self.steps:
            if step.kind == "reach":
                return step.goals
        return ()

    @property
    def goal_modules(self) -> tuple[str, ...]:
        """Every module some step names, in order of first mention."""
        return tuple(self.first_goals)

    @property
    def first_goals(self) -> dict[str, Goal]:
        """For each module, the first goal that names it."""
        found: dict[str, Goal] = {}
        for step in self.steps:
            for goal in step.goals:
                found.setdefault(goal.module, goal)
        return found

    @property
    def signature(self) -> str:
        """The task in standard terms, generated from its steps: the same whatever the wording."""
        return " | ".join(step.signature() for step in self.steps)

    @property
    def instruction(self) -> str:
        """The default wording: the first one given, or a generated sentence."""
        if self.instructions:
            return self.instructions[0]
        return _compose([step.say[0] if step.say else self.plain_wording(step) for step in self.steps])

    def sample_instruction(self, rng: random.Random | None = None) -> str:
        """One wording of the whole task, picked from the alternatives given."""
        rng = rng or random.Random()
        if self.instructions:
            return rng.choice(self.instructions)
        return _compose([rng.choice(step.say) if step.say else self.plain_wording(step) for step in self.steps])

    def plain_wording(self, step: Step) -> str:
        """A plain sentence for a step, generated from the step itself."""
        if step.kind == "hold":
            unit = "second" if step.hold_s == 1 else "seconds"
            return f"Wait {step.hold_s:g} {unit}"
        back = [goal for goal in step.goals if goal.from_start]
        fixed = [goal for goal in step.goals if not goal.from_start]
        parts = []
        if fixed:
            parts.append("Set " + " and ".join(self._goal_phrase(goal) for goal in fixed))
        if back:
            names = " and ".join(self._module_phrase(goal.module) for goal in back)
            verb = "put" if parts else "Put"
            parts.append(f"{verb} {names} back where {'it' if len(back) == 1 else 'they'} started")
        return " and ".join(parts)

    def _module_phrase(self, module: str) -> str:
        spec = self.specs.get(module)
        return spec.phrase if spec else f"the {module}"

    def _goal_phrase(self, goal: Goal) -> str:
        spec = self.specs.get(goal.module)
        say = spec.phrase_value if spec else (lambda value: f"{value:g}")
        name = self._module_phrase(goal.module)
        if goal.target is not None:
            return f"{name} to {say(goal.target)}"
        low_open = spec is not None and goal.low <= spec.stops[0]
        high_open = spec is not None and goal.high >= spec.stops[1]
        if high_open and not low_open:
            return f"{name} to at least {say(goal.low)}"
        if low_open and not high_open:
            return f"{name} to at most {say(goal.high)}"
        return f"{name} to between {say(goal.low)} and {say(goal.high)}"

    def sample_start(self, rng: random.Random | None = None) -> dict[str, float]:
        """One start state: a scale value for every module the task places.

        A start in a goal module is always at least the goal's margin away
        from the first goal for that module, so no episode begins already solved.
        """
        rng = rng or random.Random()
        goals = self.first_goals
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
            raise TaskError("a task must be a mapping of settings (name, steps or goal, start, ...)")
        by_name = {spec.name: spec for spec in specs}

        unknown = sorted(set(map(str, data)) - _TOP_KEYS)
        if unknown:
            raise TaskError(f"unknown setting(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(_TOP_KEYS))}")

        name = data.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise TaskError("'name' is required: lower-case letters, digits, '-' and '_'")
        instructions = _wordings(name, "instruction", data.get("instruction"))

        if data.get("steps") is not None:
            if data.get("goal") is not None:
                raise TaskError(f"{name}: give either 'goal' (one step) or 'steps', not both")
            steps = _parse_steps(name, data["steps"], by_name)
        else:
            steps = (Step("reach", goals=_parse_goals(name, "goal", data.get("goal"), by_name)),)

        start = _parse_start(name, data.get("start"), by_name, steps)
        _check_step_order(name, steps, start, by_name)

        others = data.get("others", "unchanged")
        if others not in ("unchanged", "ignore"):
            raise TaskError(f"{name}: 'others' must be 'unchanged' or 'ignore', got {others!r}")
        named = {goal.module for step in steps for goal in step.goals}
        others_tol = _parse_others_tol(name, data.get("others_tol"), by_name, named)

        hold_s = _number(name, "hold_s", data.get("hold_s", 1.0))
        settle_s = _number(name, "settle_s", data.get("settle_s", 0.5))
        budget = sum(step.hold_s if step.kind == "hold" else (step.within_s or DEFAULT_STEP_BUDGET_S) for step in steps)
        if len(steps) > 1:
            budget += hold_s + settle_s * (len(steps) - 1)  # time to confirm each step
        timeout_s = _number(name, "timeout_s", data.get("timeout_s", budget))
        if hold_s < 0:
            raise TaskError(f"{name}: 'hold_s' cannot be negative")
        if settle_s < 0:
            raise TaskError(f"{name}: 'settle_s' cannot be negative")
        if timeout_s <= hold_s:
            raise TaskError(f"{name}: 'timeout_s' ({timeout_s:g}) must be longer than 'hold_s' ({hold_s:g})")
        holds = sum(step.hold_s for step in steps if step.kind == "hold")
        if timeout_s < holds:
            raise TaskError(
                f"{name}: 'timeout_s' ({timeout_s:g}) leaves no time after the hold steps, which take {holds:g} s"
            )

        return cls(
            name=name,
            steps=steps,
            start=start,
            instructions=instructions,
            others=others,
            others_tol=others_tol,
            hold_s=hold_s,
            settle_s=settle_s,
            timeout_s=timeout_s,
            source=dict(data),
            specs=by_name,
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


# Tables


def _cell(text: str) -> Any:
    """A table cell as a YAML-ready value: a number where it is one, otherwise the text."""
    text = text.strip()
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def table_to_dict(text: str, name: str, specs: Iterable[ModuleSpec] = DEFAULT_MODULES) -> dict:
    """Turn a task written as a table (CSV) into the task mapping that ``Task.from_dict`` reads.

    One row per step. Columns: ``kind`` (``start``, ``reach`` or ``hold``),
    ``say``, one column per module, and optionally ``tol_<module>``,
    ``within_s``, ``hold_s`` and ``early_s``. An empty module cell means
    "leave it as it is".

    In a ``start`` row a module cell is a position, ``random``, ``a..b`` for a
    range or ``a|b|c`` for a choice. In a ``reach`` row it is a position, a
    marking, ``start``, ``>=75`` or ``<=25``. Several wordings go in one
    ``say`` cell separated by ``|``.
    """
    import csv
    import io

    modules = [spec.name for spec in specs]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "kind" not in [field.strip() for field in reader.fieldnames]:
        raise TaskError("the table needs a header row with at least a 'kind' column")
    allowed = {"kind", "say", "within_s", "hold_s", "early_s", *modules, *(f"tol_{module}" for module in modules)}
    unknown = sorted({field.strip() for field in reader.fieldnames} - allowed)
    if unknown:
        raise TaskError(f"unknown column(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}")

    data: dict[str, Any] = {"name": name}
    steps: list[dict[str, Any]] = []
    for number, raw in enumerate(reader, start=2):
        row = {key.strip(): (value or "").strip() for key, value in raw.items() if key}
        if not any(row.values()):
            continue
        kind = row.get("kind", "").lower()
        cells = {module: row[module] for module in modules if row.get(module)}
        if kind == "start":
            start = {}
            for module, cell in cells.items():
                if ".." in cell:
                    low, _, high = cell.partition("..")
                    start[module] = {"range": [_cell(low), _cell(high)]}
                elif "|" in cell:
                    start[module] = {"sample": [_cell(part) for part in cell.split("|")]}
                else:
                    start[module] = _cell(cell)
            data["start"] = start
            continue
        step: dict[str, Any] = {}
        if row.get("say"):
            wordings = [part.strip() for part in row["say"].split("|") if part.strip()]
            step["say"] = wordings if len(wordings) > 1 else wordings[0]
        if kind == "reach":
            reach = {}
            for module, cell in cells.items():
                tol = row.get(f"tol_{module}")
                if cell.startswith(">="):
                    reach[module] = {"min": _cell(cell[2:])}
                elif cell.startswith("<="):
                    reach[module] = {"max": _cell(cell[2:])}
                elif tol:
                    reach[module] = {"target": _cell(cell), "tol": _cell(tol)}
                else:
                    reach[module] = _cell(cell)
            if not reach:
                raise TaskError(f"row {number}: a reach step needs a position in at least one module column")
            step["reach"] = reach
            if row.get("within_s"):
                step["within_s"] = _cell(row["within_s"])
        elif kind == "hold":
            if not row.get("hold_s"):
                raise TaskError(f"row {number}: a hold step needs 'hold_s'")
            step["hold_s"] = _cell(row["hold_s"])
            if row.get("early_s"):
                step["early_s"] = _cell(row["early_s"])
        else:
            raise TaskError(f"row {number}: 'kind' must be start, reach or hold, got {row.get('kind')!r}")
        steps.append(step)
    if not steps:
        raise TaskError("the table has no reach or hold rows")
    data["steps"] = steps
    Task.from_dict(data, specs)  # reject a table that does not make a valid task
    return data


# Wording


def _compose(phrases: list[str]) -> str:
    """One instruction from the wordings of the steps, in order."""
    cleaned = [phrase.strip().rstrip(".").strip() for phrase in phrases]
    if len(cleaned) == 1:
        return phrases[0].strip()
    rest = [phrase[:1].lower() + phrase[1:] for phrase in cleaned[1:]]
    return ", then ".join([cleaned[0], *rest]) + "."


def _wordings(task: str, what: str, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    if not items or not all(isinstance(item, str) and item.strip() for item in items):
        raise TaskError(f"{task}: '{what}' must be a sentence or a list of sentences, and not empty")
    return tuple(item.strip() for item in items)


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


def _is_start_word(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "start"


def _parse_goals(task: str, section: str, raw: Any, by_name: Mapping[str, ModuleSpec]) -> tuple[Goal, ...]:
    if not isinstance(raw, dict) or not raw:
        raise TaskError(f"{task}: '{section}' is required and must name at least one module")
    goals = []
    for module, entry in raw.items():
        spec = _spec(task, section, module, by_name)
        if _is_start_word(entry):
            goals.append(Goal(spec.name, -spec.snap, spec.snap, margin=spec.snap, from_start=True))
            continue
        if not isinstance(entry, dict):
            target = _resolve(task, section, spec, entry)
            goals.append(Goal(spec.name, target - spec.snap, target + spec.snap, target, margin=spec.snap))
            continue
        keys = set(entry)
        if "target" in keys:
            if keys - {"target", "tol"}:
                raise TaskError(f"{task}: {section} {module}: with 'target' only 'tol' may be added, got {sorted(keys)}")
            tol = _number(task, f"{section} {module} tol", entry.get("tol", spec.snap))
            if tol <= 0:
                raise TaskError(f"{task}: {section} {module}: 'tol' must be greater than 0")
            if _is_start_word(entry["target"]):
                goals.append(Goal(spec.name, -tol, tol, margin=tol, from_start=True))
            else:
                target = _resolve(task, section, spec, entry["target"])
                goals.append(Goal(spec.name, target - tol, target + tol, target, margin=tol))
        elif keys and keys <= {"min", "max"}:
            low = _number(task, f"{section} {module} min", entry["min"]) if "min" in keys else spec.stops[0]
            high = _number(task, f"{section} {module} max", entry["max"]) if "max" in keys else spec.stops[1]
            if high < spec.stops[0] or low > spec.stops[1]:
                given = " and ".join(f"{key} {entry[key]:g}" for key in ("min", "max") if key in keys)
                raise TaskError(
                    f"{task}: {section} {module}: {given} is out of reach "
                    f"(the module travels {spec.stops[0]:g} to {spec.stops[1]:g})"
                )
            if not low < high:
                raise TaskError(f"{task}: {section} {module}: 'min' ({low:g}) must be below 'max' ({high:g})")
            goals.append(Goal(spec.name, low, high, margin=spec.snap))
        else:
            raise TaskError(
                f"{task}: {section} {module}: give a value, {{target, tol}}, {{min, max}} or 'start'; "
                f"got {sorted(map(str, keys))}"
            )
    return tuple(goals)


def _parse_steps(task: str, raw: Any, by_name: Mapping[str, ModuleSpec]) -> tuple[Step, ...]:
    if not isinstance(raw, list) or not raw:
        raise TaskError(f"{task}: 'steps' must be a list with at least one step")
    steps = []
    for index, entry in enumerate(raw):
        where = f"step {index}"
        if not isinstance(entry, dict):
            raise TaskError(f"{task}: {where}: a step is a mapping with 'reach' or 'hold_s'")
        unknown = sorted(set(map(str, entry)) - _STEP_KEYS)
        if unknown:
            raise TaskError(f"{task}: {where}: unknown setting(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(_STEP_KEYS))}")
        say = _wordings(task, f"{where} say", entry.get("say"))
        is_reach, is_hold = entry.get("reach") is not None, entry.get("hold_s") is not None
        if is_reach == is_hold:
            raise TaskError(f"{task}: {where}: give exactly one of 'reach' (do something) or 'hold_s' (wait)")
        if is_reach:
            if entry.get("early_s") is not None:
                raise TaskError(f"{task}: {where}: 'early_s' belongs to a hold step")
            within = entry.get("within_s")
            if within is not None:
                within = _number(task, f"{where} within_s", within)
                if within <= 0:
                    raise TaskError(f"{task}: {where}: 'within_s' must be greater than 0")
            steps.append(Step("reach", goals=_parse_goals(task, f"{where} reach", entry["reach"], by_name), say=say, within_s=within))
        else:
            if entry.get("within_s") is not None:
                raise TaskError(f"{task}: {where}: 'within_s' belongs to a reach step; the step after a hold sets how late the robot may be")
            hold = _number(task, f"{where} hold_s", entry["hold_s"])
            early = _number(task, f"{where} early_s", entry.get("early_s", 0.0))
            if hold <= 0:
                raise TaskError(f"{task}: {where}: 'hold_s' must be greater than 0")
            if not 0 <= early < hold:
                raise TaskError(f"{task}: {where}: 'early_s' must be 0 or more and less than 'hold_s'")
            steps.append(Step("hold", say=say, hold_s=hold, early_s=early))
    return tuple(steps)


def _parse_start(
    task: str, raw: Any, by_name: Mapping[str, ModuleSpec], steps: tuple[Step, ...]
) -> dict[str, Start]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TaskError(f"{task}: 'start' must map module names to start positions")
    first_goals: dict[str, Goal] = {}
    for step in steps:
        for goal in step.goals:
            first_goals.setdefault(goal.module, goal)

    start: dict[str, Start] = {}
    for module, entry in raw.items():
        spec = _spec(task, "start", module, by_name)
        goal = first_goals.get(spec.name)
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

        if goal is not None and not goal.from_start:
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

    missing = [module for module in first_goals if module not in start]
    if missing:
        raise TaskError(
            f"{task}: every goal module needs a start, otherwise an episode could begin solved; "
            f"missing: {', '.join(missing)}"
        )
    return start


def _check_step_order(
    task: str, steps: tuple[Step, ...], start: Mapping[str, Start], by_name: Mapping[str, ModuleSpec]
) -> None:
    """Reject steps that would already be done the moment they begin."""
    latest: dict[str, Goal] = {}
    for index, step in enumerate(steps):
        if step.kind != "reach":
            continue
        already = []
        for goal in step.goals:
            before = latest.get(goal.module)
            if goal.from_start:
                if before is None:
                    raise TaskError(
                        f"{task}: step {index}: '{goal.module}: start' only makes sense after an earlier step "
                        "has moved that module"
                    )
                placed = start[goal.module]
                if placed.kind != "range" and not before.from_start:
                    already.append(any(before.distance(value) <= goal.high for value in placed.values))
                else:
                    already.append(False)
            elif before is not None and not before.from_start:
                already.append(before.low <= goal.high and goal.low <= before.high)
            else:
                already.append(False)
        if already and all(already) and any(latest.get(goal.module) is not None for goal in step.goals):
            names = ", ".join(goal.module for goal in step.goals)
            raise TaskError(
                f"{task}: step {index}: {names} would already be in this goal when the step begins, "
                "so the step asks for nothing"
            )
        for goal in step.goals:
            latest[goal.module] = goal


def _parse_others_tol(
    task: str, raw: Any, by_name: Mapping[str, ModuleSpec], goal_modules: set[str]
) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TaskError(f"{task}: 'others_tol' must map module names to tolerances")
    result = {}
    for module, value in raw.items():
        spec = _spec(task, "others_tol", module, by_name)
        if spec.name in goal_modules:
            raise TaskError(f"{task}: others_tol {module}: this is a goal module, set its 'tol' under its goal")
        tol = _number(task, f"others_tol {module}", value)
        if tol <= 0:
            raise TaskError(f"{task}: others_tol {module}: must be greater than 0")
        result[spec.name] = tol
    return result


def _sample_range(bounds: tuple[float, ...], goal: Goal | None, rng: random.Random) -> float:
    low, high = bounds
    if goal is None or goal.from_start:
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
