"""The scorer: turns a task and a trace into an outcome.

Scoring is a pure function of the task and the encoder trace. Nobody labels
an episode by hand, and the same trace always gives the same result.

Outcomes
    success       goal reached and held, nothing else disturbed
    late          in the goal when the episode ended, but not yet held long enough
    side_effect   goal reached, but another module was moved and left displaced
    overshoot     the goal was reached at some point but not held
    partial       a goal module was moved, the goal was never reached
    wrong_module  only modules that are not part of the goal were moved
    no_contact    nothing moved
    aborted       the episode was stopped from outside (stall, safety, operator)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from .modules import DEFAULT_MODULES, ModuleSpec
from .task import Task
from .trace import Sample

OUTCOMES = (
    "success",
    "late",
    "side_effect",
    "overshoot",
    "partial",
    "wrong_module",
    "no_contact",
    "aborted",
)

MOVED_FRACTION_OF_SNAP = 0.25
"""A module counts as moved once it is this fraction of its ``snap`` from where it started."""


_EPSILON = 1e-9
"""Slack for comparing times, so 2.9 - 1.9 counts as a full second."""


class ScoreError(ValueError):
    """The trace cannot be scored against this task."""


@dataclass(frozen=True)
class Result:
    task: str
    outcome: str
    success: bool
    duration_s: float
    """Time from the first sample to the end of the scored episode."""
    time_to_goal_s: float | None
    """When the goal was entered for the hold that succeeded."""
    progress: float
    """0 at the start position, 1 in the goal, at the end of the episode."""
    best_progress: float
    """The closest the episode ever came."""
    final_error: Mapping[str, float]
    """Per goal module, how far outside the goal it ended (0 inside)."""
    moved: tuple[str, ...]
    """Modules that moved at any point."""
    disturbed: tuple[str, ...]
    """Modules outside the goal that ended away from where they started."""
    start: Mapping[str, float]
    final: Mapping[str, float]
    abort_reason: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["moved"] = list(self.moved)
        data["disturbed"] = list(self.disturbed)
        return data


class Scorer:
    """Scores one episode sample by sample.

    Feed it ``update(t, values)`` in time order. It returns ``True`` once the
    episode is over: the task succeeded or the timeout passed. ``result()``
    gives the outcome at any point.
    """

    def __init__(self, task: Task, specs: Iterable[ModuleSpec] = DEFAULT_MODULES):
        self.task = task
        self._specs = {spec.name: spec for spec in specs}
        missing = [name for name in task.goal_modules if name not in self._specs]
        if missing:
            raise ScoreError(f"task {task.name} needs module(s) this board does not have: {', '.join(missing)}")
        self._goal_modules = set(task.goal_modules)
        self._moved_tol = {name: spec.snap * MOVED_FRACTION_OF_SNAP for name, spec in self._specs.items()}
        self._disturb_tol = {name: task.others_tol.get(name, spec.snap) for name, spec in self._specs.items()}

        self._t0: float | None = None
        self._last_t: float | None = None
        self._elapsed = 0.0
        self._start: dict[str, float] = {}
        self._final: dict[str, float] = {}
        self._max_deviation: dict[str, float] = {}
        self._ever_in_goal = False
        self._full_since: float | None = None
        self._best_progress = 0.0
        self._time_to_goal: float | None = None
        self._done = False
        self._abort_reason: str | None = None

    @property
    def done(self) -> bool:
        return self._done

    def update(self, t: float, values: Mapping[str, float]) -> bool:
        if self._done:
            return True
        missing = [name for name in self._specs if name not in values]
        if missing:
            raise ScoreError(f"sample at t={t:g} has no value for: {', '.join(missing)}")
        state = {name: float(values[name]) for name in self._specs}

        if self._t0 is None:
            self._t0 = self._last_t = t
            self._start = dict(state)
            self._max_deviation = {name: 0.0 for name in state}
        elif t < self._last_t:
            raise ScoreError(f"samples must be in time order: t={t:g} comes after t={self._last_t:g}")

        elapsed = t - self._t0
        if elapsed > self.task.timeout_s + _EPSILON:
            # Past the timeout: the episode ended at the timeout, this sample is not part of it.
            self._elapsed = self.task.timeout_s
            self._done = True
            return True

        self._last_t = t
        self._elapsed = elapsed
        self._final = state
        for name, value in state.items():
            self._max_deviation[name] = max(self._max_deviation[name], abs(value - self._start[name]))

        goal_ok = self._goal_ok(state)
        self._ever_in_goal = self._ever_in_goal or goal_ok
        self._best_progress = max(self._best_progress, self._progress(state))

        if goal_ok and not self._disturbed(state):
            if self._full_since is None:
                self._full_since = elapsed
            if elapsed - self._full_since >= self.task.hold_s - _EPSILON:
                self._time_to_goal = self._full_since
                self._done = True
        else:
            self._full_since = None

        if elapsed >= self.task.timeout_s - _EPSILON:
            self._done = True
        return self._done

    def abort(self, reason: str) -> None:
        """Stop the episode from outside, e.g. on a stall or an operator stop."""
        if not self._done:
            self._abort_reason = reason or "aborted"
            self._done = True

    def result(self) -> Result:
        if self._t0 is None:
            raise ScoreError("nothing to score: no samples yet")
        final = self._final
        moved = tuple(name for name in self._specs if self._max_deviation[name] > self._moved_tol[name])
        disturbed = tuple(self._disturbed(final))
        return Result(
            task=self.task.name,
            outcome=self._outcome(moved, disturbed),
            success=self._time_to_goal is not None and self._abort_reason is None,
            duration_s=self._elapsed,
            time_to_goal_s=self._time_to_goal if self._abort_reason is None else None,
            progress=self._progress(final),
            best_progress=self._best_progress,
            final_error={goal.module: goal.distance(final[goal.module]) for goal in self.task.goals},
            moved=moved,
            disturbed=disturbed,
            start=dict(self._start),
            final=dict(final),
            abort_reason=self._abort_reason,
        )

    def _goal_ok(self, state: Mapping[str, float]) -> bool:
        return all(goal.satisfied(state[goal.module]) for goal in self.task.goals)

    def _disturbed(self, state: Mapping[str, float]) -> list[str]:
        if self.task.others == "ignore":
            return []
        return [
            name
            for name in self._specs
            if name not in self._goal_modules and abs(state[name] - self._start[name]) > self._disturb_tol[name]
        ]

    def _progress(self, state: Mapping[str, float]) -> float:
        parts = []
        for goal in self.task.goals:
            start_distance = goal.distance(self._start[goal.module])
            distance = goal.distance(state[goal.module])
            if start_distance <= 0.0:
                parts.append(1.0 if distance <= 0.0 else 0.0)
            else:
                parts.append(min(1.0, max(0.0, 1.0 - distance / start_distance)))
        return sum(parts) / len(parts)

    def _outcome(self, moved: tuple[str, ...], disturbed: tuple[str, ...]) -> str:
        if self._abort_reason is not None:
            return "aborted"
        if self._time_to_goal is not None:
            return "success"
        if self._goal_ok(self._final):
            return "side_effect" if disturbed else "late"
        if not moved:
            return "no_contact"
        if not self._goal_modules.intersection(moved):
            return "wrong_module"
        return "overshoot" if self._ever_in_goal else "partial"


def score(
    task: Task,
    samples: Iterable[Sample],
    specs: Iterable[ModuleSpec] = DEFAULT_MODULES,
    abort_reason: str | None = None,
) -> Result:
    """Score a recorded trace. ``abort_reason`` marks an episode that was stopped from outside."""
    scorer = Scorer(task, specs)
    for sample in samples:
        if scorer.update(sample.t, sample.values):
            break
    if abort_reason is not None:
        scorer.abort(abort_reason)
    return scorer.result()
