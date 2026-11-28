"""Step codes: a whole task on one line.

A code is a tile letter and the number printed on the tile::

    A0 A1 A2              switch at O, I, II (the printed symbols read as 0, 1, 2)
    B0 B25 B50 B75 B100   valve at CLOSED, 25, 50, 75, OPEN
    C0 C2 C4              plunger pushed in, half way, pulled out (rings 0, 2, 4)
    D1 .. D5              slider at marks 1 to 5

These sixteen are the standard states. The valve, plunger and slider also
take values in between (``B37``, ``C1``, ``D2.5``); those are legal but not
standard.

Three more codes::

    W30        wait 30 seconds
    AS         put tile A back where it started
    B50+D5     both in the same step

A task is its steps in order, optionally with a start state before a ``>``::

    A1 B100 C4 D1 > C0 A2 D4 AS B0

A tile that the steps use but the start does not mention starts at one of
its other standard states, picked per episode.

Everything a code does not say is fixed here, so the same line always makes
the same task: the tile's usual tolerance, ``STEP_TIME_S`` for each action,
and a wait that may be left up to ``EARLY_FRACTION`` of its length early.
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources
from typing import Any, Iterable

import yaml

from .modules import DEFAULT_MODULES, ModuleError, ModuleSpec
from .task import Task, TaskError

STEP_TIME_S = 20.0
"""Time allowed for each action in a task built from codes."""
EARLY_FRACTION = 0.1
"""How much of a wait may be skipped: W30 may be left from 27 s on."""

_NUMBER = r"\d+(?:\.\d+)?"
_PART = re.compile(rf"([A-Za-z])(?:([Ss])|({_NUMBER}))")
_WAIT = re.compile(rf"[Ww]({_NUMBER})")


def looks_like_codes(text: str) -> bool:
    """Whether a string is written in step codes (so it is not a task name or a file)."""
    tokens = text.replace(",", " ").replace(">", " > ").split()
    if not tokens:
        return False
    return all(
        token == ">" or _WAIT.fullmatch(token) or all(_PART.fullmatch(part) for part in token.split("+"))
        for token in tokens
    )


@lru_cache(maxsize=None)
def _wordings() -> dict[str, Any]:
    text = resources.files("lebox.library").joinpath("wordings.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _number(text: str) -> float | int:
    value = float(text)
    return int(value) if value.is_integer() else value


def _by_letter(specs: Iterable[ModuleSpec]) -> dict[str, ModuleSpec]:
    return {spec.letter.upper(): spec for spec in specs if spec.letter}


def _position(spec: ModuleSpec, number: float) -> float:
    try:
        return spec.resolve(spec.value_for_code(number))
    except ModuleError as exc:
        raise TaskError(f"{spec.letter}{number:g}: {exc}") from None


def _tile(letter: str, tiles: dict[str, ModuleSpec], token: str) -> ModuleSpec:
    spec = tiles.get(letter.upper())
    if spec is None:
        raise TaskError(f"'{token}': there is no tile {letter.upper()} (tiles: {', '.join(sorted(tiles))})")
    return spec


def _step(token: str, tiles: dict[str, ModuleSpec]) -> dict[str, Any]:
    wait = _WAIT.fullmatch(token)
    if wait and "W" not in tiles:
        seconds = _number(wait.group(1))
        if seconds <= 0:
            raise TaskError(f"'{token}': a wait must be longer than 0 seconds")
        step: dict[str, Any] = {"hold_s": seconds}
        early = round(seconds * EARLY_FRACTION, 1)
        if early:
            step["early_s"] = _number(str(early))
        step["say"] = [sentence.format(n=f"{seconds:g}") for sentence in _wordings()["wait"]]
        return step

    reach: dict[str, Any] = {}
    say: list[list[str]] = []
    for part in token.split("+"):
        match = _PART.fullmatch(part)
        if not match:
            raise TaskError(f"'{part}' is not a step code; write a tile letter and a number (B50), a wait (W30) or a put-back (AS)")
        letter, back, number = match.groups()
        spec = _tile(letter, tiles, part)
        if spec.name in reach:
            raise TaskError(f"'{token}': tile {spec.letter} appears twice in one step")
        if back:
            reach[spec.name] = "start"
            say.append(list(_wordings()["back"].get(spec.letter, [])))
        else:
            value = _position(spec, _number(number))
            reach[spec.name] = int(value) if float(value).is_integer() else value
            say.append(list(_wordings().get(spec.code_for_value(value) or "", [])))
    step = {"reach": reach, "within_s": _number(str(STEP_TIME_S))}
    if len(say) == 1 and say[0]:
        step["say"] = say[0]
    return step


def codes_to_dict(text: str, name: str | None = None, specs: Iterable[ModuleSpec] = DEFAULT_MODULES) -> dict[str, Any]:
    """Turn a line of step codes into the task mapping that ``Task.from_dict`` reads."""
    specs = tuple(specs)
    tiles = _by_letter(specs)
    start_text, arrow, steps_text = text.replace(",", " ").rpartition(">")
    if arrow and ">" in start_text:
        raise TaskError("a task has at most one '>': start state on the left, steps on the right")

    start: dict[str, Any] = {}
    for token in start_text.split():
        match = _PART.fullmatch(token)
        if not match or match.group(2):
            raise TaskError(f"'{token}' cannot be part of a start state; write a tile letter and a number, such as B100")
        spec = _tile(match.group(1), tiles, token)
        if spec.name in start:
            raise TaskError(f"the start state gives tile {spec.letter} twice")
        value = _position(spec, _number(match.group(3)))
        start[spec.name] = int(value) if float(value).is_integer() else value

    tokens = steps_text.split()
    if not tokens:
        raise TaskError("no steps: write at least one code, such as D3")
    steps = [_step(token, tiles) for token in tokens]

    # A tile the steps use but the start leaves out begins at one of its other standard states.
    by_name = {spec.name: spec for spec in specs}
    first_target: dict[str, Any] = {}
    for step in steps:
        for module, target in step.get("reach", {}).items():
            first_target.setdefault(module, target)
    for module, target in first_target.items():
        if module in start:
            continue
        spec = by_name[module]
        options = []
        for number in spec.standard_codes:
            value = min(spec.work[1], max(spec.work[0], spec.value_for_code(number)))
            if target == "start" or abs(value - float(target)) > 2 * spec.tolerance:
                options.append(int(value) if float(value).is_integer() else value)
        start[module] = {"sample": options}

    canonical = " ".join(token.upper() for token in tokens)
    data: dict[str, Any] = {"name": name or _name_for(canonical), "start": start, "steps": steps}
    Task.from_dict(data, specs)  # reject a line that does not make a valid task
    return data


def task_from_codes(text: str, name: str | None = None, specs: Iterable[ModuleSpec] = DEFAULT_MODULES) -> Task:
    """A task from a line of step codes, such as ``"A1 B100 C4 D1 > C0 A2 D4 AS B0"``."""
    return Task.from_dict(codes_to_dict(text, name, specs), specs)


def _name_for(canonical: str) -> str:
    return canonical.lower().replace(" ", "_").replace("+", "-").replace(".", "p")


def codes_for(task: Task) -> str | None:
    """The steps of a task as codes, or ``None`` if a step is not a standard one.

    A step is standard when it uses the tile's usual tolerance and the
    library's times. Only then does the code say everything about it.
    """
    tokens = []
    for step in task.steps:
        if step.kind == "hold":
            expected_early = round(step.hold_s * EARLY_FRACTION, 1)
            if abs(step.early_s - expected_early) > 1e-9:
                return None
            tokens.append(f"W{step.hold_s:g}")
            continue
        if step.within_s is None or abs(step.within_s - STEP_TIME_S) > 1e-9:
            return None
        parts = []
        for goal in step.goals:
            spec = task.specs.get(goal.module)
            if spec is None or not spec.letter:
                return None
            if goal.from_start:
                if abs(goal.high - spec.tolerance) > 1e-9:
                    return None
                parts.append(f"{spec.letter}S")
                continue
            if goal.target is None or abs((goal.high - goal.low) / 2 - spec.tolerance) > 1e-9:
                return None
            code = spec.code_for_value(goal.target)
            if code is None:
                return None
            parts.append(code)
        tokens.append("+".join(parts))
    return " ".join(tokens)


def standard_steps(specs: Iterable[ModuleSpec] = DEFAULT_MODULES) -> list[tuple[str, str, list[str]]]:
    """The standard library: (code, plain sentence, alternative sentences) for every standard state."""
    library = _wordings()
    rows = []
    for spec in specs:
        if not spec.letter:
            continue
        for number in spec.standard_codes:
            code = f"{spec.letter}{number:g}"
            sentences = list(library.get(code, []))
            rows.append((code, sentences[0] if sentences else "", sentences[1:]))
    return rows
