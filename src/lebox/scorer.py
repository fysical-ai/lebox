"""The scorer: turns a task and a trace into an outcome.

Scoring is a pure function of the task and the encoder trace. Nobody labels
an episode by hand, and the same trace always gives the same result.

A task is a list of steps, and the scorer works through them in order.

A reach step is done once its goal has been reached and has held for a
moment. Everything else must be in place at the moment the goal is reached:
modules that earlier steps set must still be where those steps left them,
and modules that only later steps name must not have been moved yet. That
is what keeps the steps in order. Once the goal is reached, the robot is
free to start on the next step while this one is being confirmed.

A hold step is done once nothing has moved for its duration, counted from
the moment the step before it was reached. Moving on up to ``early_s`` before
the end is accepted; moving on sooner fails the episode.

Clocks are relative: each step's time window starts when the step before
it was reached.

Outcomes
    success       every step done, the last goal held, nothing else disturbed
    late          in the goal when the episode ended, but not yet held long enough
    side_effect   goal reached, but another module was moved and left displaced
    overshoot     the goal was reached at some point but not held
    partial       a goal module was moved, the goal was never reached
    wrong_module  only modules that are not part of the goal were moved
    wrong_order   a later step was carried out before the present one
    too_early     the robot moved on before a wait was over
    no_contact    nothing moved
    aborted       the episode was stopped from outside (stall, safety, operator)

For a task with several steps, the outcome describes the step the episode
stopped at; ``steps_completed`` says how far it got.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from .modules import DEFAULT_MODULES, ModuleSpec
from .task import Goal, Step, Task
from .trace import Sample

OUTCOMES = (
    "success",
    "late",
    "side_effect",
    "overshoot",
    "partial",
    "wrong_module",
    "wrong_order",
    "too_early",
    "no_contact",
    "aborted",
)

MOVED_FRACTION_OF_SNAP = 0.25
"""A module counts as moved once it is this fraction of its ``snap`` from where it started."""

_EPSILON = 1e-9
"""Slack for comparing times, so 2.9 - 1.9 counts as a full second."""

Regions = dict[str, tuple[float, float]]


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
    """When the final goal was reached, for an episode that succeeded."""
    progress: float
    """0 at the start, 1 when every step is done. Each step counts equally."""
    best_progress: float
    """The furthest the episode ever got."""
    final_error: Mapping[str, float]
    """Per goal module of the step the episode ended on, how far outside the goal it ended (0 inside)."""
    moved: tuple[str, ...]
    """Modules that moved at any point."""
    disturbed: tuple[str, ...]
    """Modules that ended away from where they should have been left."""
    start: Mapping[str, float]
    final: Mapping[str, float]
    abort_reason: str | None = None
    steps_total: int = 1
    steps_completed: int = 0
    failed_step: int | None = None
    """The step (counted from 0) the episode stopped at, when it did not succeed."""
    step_times: tuple[float, ...] = ()
    """When each completed step was reached, in seconds from the start."""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["moved"] = list(self.moved)
        data["disturbed"] = list(self.disturbed)
        data["step_times"] = list(self.step_times)
        return data


def _inside(regions: Regions, state: Mapping[str, float], skip: frozenset[str] = frozenset()) -> bool:
    return all(low <= state[name] <= high for name, (low, high) in regions.items() if name not in skip)


class Scorer:
    """Scores one episode sample by sample.

    Feed it ``update(t, values)`` in time order. It returns ``True`` once the
    episode is over: the task succeeded, a step failed for good, or the
    timeout passed. ``result()`` gives the outcome at any point.
    """

    def __init__(self, task: Task, specs: Iterable[ModuleSpec] = DEFAULT_MODULES):
        self.task = task
        self._specs = {spec.name: spec for spec in specs}
        missing = [name for name in task.goal_modules if name not in self._specs]
        if missing:
            raise ScoreError(f"task {task.name} needs module(s) this board does not have: {', '.join(missing)}")
        self._moved_tol = {name: spec.snap * MOVED_FRACTION_OF_SNAP for name, spec in self._specs.items()}
        self._disturb_tol = {name: task.others_tol.get(name, spec.snap) for name, spec in self._specs.items()}

        self.steps: tuple[Step, ...] = task.steps
        """The task's steps; goals written as ``start`` are fixed once the first sample is in."""
        self.current_step = 0
        self.step_clock = 0.0
        """When the present step's time window began, in seconds from the start."""

        self._t0: float | None = None
        self._last_t: float | None = None
        self._elapsed = 0.0
        self._start: dict[str, float] = {}
        self._final: dict[str, float] = {}
        self._max_deviation: dict[str, float] = {}
        self._before: list[Regions] = []
        self._after: list[Regions] = []
        self._later: list[frozenset[str]] = []
        self._streak: float | None = None
        self._streak_state: dict[str, float] = {}
        self._broken_at: float | None = None
        self._baseline: dict[str, float] = {}
        self._step_deviation: dict[str, float] = {}
        self._ever_in_goal = False
        self._step_times: list[float] = []
        self._best_progress = 0.0
        self._time_to_goal: float | None = None
        self._failure: str | None = None
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
            self._begin(t, state)
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
            self._step_deviation[name] = max(self._step_deviation[name], abs(value - self._baseline[name]))

        # One sample can finish a step and start the next, so keep going until nothing changes.
        while not self._done and self._advance(elapsed, state):
            pass
        self._best_progress = max(self._best_progress, self._progress(state))

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
        succeeded = self._time_to_goal is not None and self._abort_reason is None
        moved = tuple(name for name in self._specs if self._max_deviation[name] > self._moved_tol[name])
        out_of_place = () if self._time_to_goal is not None else tuple(self._out_of_place(final))
        step = self.steps[min(self.current_step, len(self.steps) - 1)]
        return Result(
            task=self.task.name,
            outcome=self._outcome(out_of_place),
            success=succeeded,
            duration_s=self._elapsed,
            time_to_goal_s=self._time_to_goal if succeeded else None,
            progress=self._progress(final),
            best_progress=self._best_progress,
            final_error={goal.module: goal.distance(final[goal.module]) for goal in step.goals},
            moved=moved,
            disturbed=out_of_place,
            start=dict(self._start),
            final=dict(final),
            abort_reason=self._abort_reason,
            steps_total=len(self.steps),
            steps_completed=len(self._step_times),
            failed_step=None if self._time_to_goal is not None else self.current_step,
            step_times=tuple(self._step_times),
        )

    # Setting up

    def _begin(self, t: float, state: dict[str, float]) -> None:
        self._t0 = self._last_t = t
        self._start = dict(state)
        self._baseline = dict(state)
        self._max_deviation = {name: 0.0 for name in state}
        self._step_deviation = {name: 0.0 for name in state}
        self.steps = tuple(
            Step(
                step.kind,
                tuple(goal.resolved(state[goal.module]) for goal in step.goals),
                step.say,
                step.within_s,
                step.hold_s,
                step.early_s,
            )
            for step in self.task.steps
        )
        # Where every module should be before and after each step.
        regions: Regions = {}
        if self.task.others == "unchanged":
            regions = {name: (state[name] - tol, state[name] + tol) for name, tol in self._disturb_tol.items()}
        for index, step in enumerate(self.steps):
            self._before.append(dict(regions))
            for goal in step.goals:
                regions[goal.module] = (goal.low, goal.high)
            self._after.append(dict(regions))
            self._later.append(frozenset(g.module for later in self.steps[index + 1 :] for g in later.goals))

    # Working through the steps

    def _advance(self, elapsed: float, state: Mapping[str, float]) -> bool:
        """Look at the present step. Returns True if it was completed and another step follows."""
        index = self.current_step
        step = self.steps[index]
        last = index == len(self.steps) - 1

        if step.kind == "hold":
            in_place = _inside(self._before[index], state)
            left_at = self._broken_at if self._broken_at is not None else (None if in_place else elapsed)
            if left_at is not None:
                if left_at - self.step_clock >= step.hold_s - step.early_s - _EPSILON:
                    return self._complete(left_at, state)  # moved on, within the allowance
                self._failure = "hold"
                self._done = True
                return False
            if elapsed - self.step_clock >= step.hold_s - _EPSILON:
                return self._complete(self.step_clock + step.hold_s, state)
            return False

        goal_ok = all(goal.satisfied(state[goal.module]) for goal in step.goals)
        self._ever_in_goal = self._ever_in_goal or goal_ok
        strict = _inside(self._after[index], state)
        if self._streak is None:
            if strict:
                self._streak = elapsed
                self._streak_state = dict(state)
                self._broken_at = None
        elif not _inside(self._after[index], state, skip=self._later[index]):
            self._streak = None  # the goal, or something an earlier step set, was lost
        elif not strict and self._broken_at is None:
            self._broken_at = elapsed  # the robot has started on a later step

        if self._streak is not None:
            needed = self.task.hold_s if last else self.task.settle_s
            if elapsed - self._streak >= needed - _EPSILON:
                # The next step is measured from where things stood when this goal was reached.
                return self._complete(self._streak, self._streak_state)
        elif step.within_s is not None and elapsed - self.step_clock > step.within_s + _EPSILON:
            self._failure = "deadline"
            self._done = True
        return False

    def _complete(self, reached_at: float, state: Mapping[str, float]) -> bool:
        self._step_times.append(reached_at)
        if self.current_step == len(self.steps) - 1:
            self._time_to_goal = reached_at
            self._done = True
            return False
        carried = self._broken_at if self.steps[self.current_step].kind == "reach" else None
        self.current_step += 1
        self.step_clock = reached_at
        self._streak = None
        # A hold has to know if the robot already moved on while the last step was being confirmed.
        self._broken_at = carried if self.steps[self.current_step].kind == "hold" else None
        self._ever_in_goal = False
        self._baseline = dict(state)
        self._step_deviation = {name: abs(self._final[name] - state[name]) for name in state}
        return True

    # Describing the result

    def _out_of_place(self, state: Mapping[str, float]) -> list[str]:
        """Modules that are not where the present step expects them, its own goal modules aside."""
        index = min(self.current_step, len(self.steps) - 1)
        step = self.steps[index]
        own = {goal.module for goal in step.goals}
        regions = self._before[index] if step.kind == "hold" else self._after[index]
        return [
            name
            for name in self._specs
            if name in regions and name not in own and not regions[name][0] <= state[name] <= regions[name][1]
        ]

    def _step_progress(self, state: Mapping[str, float]) -> float:
        step = self.steps[self.current_step]
        if step.kind == "hold":
            return min(1.0, max(0.0, (self._elapsed - self.step_clock) / step.hold_s))
        parts = []
        for goal in step.goals:
            start_distance = goal.distance(self._baseline[goal.module])
            distance = goal.distance(state[goal.module])
            if start_distance <= 0.0:
                parts.append(1.0 if distance <= 0.0 else 0.0)
            else:
                parts.append(min(1.0, max(0.0, 1.0 - distance / start_distance)))
        return sum(parts) / len(parts)

    def _progress(self, state: Mapping[str, float]) -> float:
        if self._time_to_goal is not None:
            return 1.0
        return (len(self._step_times) + self._step_progress(state)) / len(self.steps)

    def _later_goal(self, index: int, module: str) -> Goal | None:
        for step in self.steps[index + 1 :]:
            for goal in step.goals:
                if goal.module == module:
                    return goal
        return None

    def _outcome(self, out_of_place: tuple[str, ...]) -> str:
        if self._abort_reason is not None:
            return "aborted"
        if self._time_to_goal is not None:
            return "success"
        index = self.current_step
        step = self.steps[index]
        final = self._final
        if step.kind == "hold":
            if self._failure != "hold":
                return "late"  # the episode ended while the wait was still running
            return "too_early" if any(name in self._later[index] for name in out_of_place) else "side_effect"

        for name in out_of_place:
            later = self._later_goal(index, name)
            if later is not None and later.satisfied(final[name]):
                return "wrong_order"
        if all(goal.satisfied(final[goal.module]) for goal in step.goals):
            return "side_effect" if out_of_place else "late"
        moved = {name for name in self._specs if self._step_deviation[name] > self._moved_tol[name]}
        if not moved:
            return "no_contact"
        if not moved.intersection(goal.module for goal in step.goals):
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
