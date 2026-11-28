"""One episode: put the start state in place, release, and score what happens."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Mapping

from .board import Board
from .motion import Clock, MotionSettings
from .scorer import Result, Scorer
from .task import Task
from .trace import Sample


@dataclass(frozen=True)
class Episode:
    task: Task
    instruction: str
    """The wording the robot was given for this episode."""
    start: Mapping[str, float]
    """The start state that was asked for (empty when there was no reset)."""
    result: Result
    samples: tuple[Sample, ...]
    interrupted: bool = False


def run_episode(
    board: Board,
    task: Task,
    clock: Clock,
    settings: MotionSettings = MotionSettings(),
    rng: random.Random | None = None,
    varied_wording: bool = True,
    reset: bool = True,
    hz: float = 20.0,
    scorer: Scorer | None = None,
    on_ready: Callable[[Mapping[str, float], str], None] | None = None,
    on_sample: Callable[[float, Mapping[str, float], Scorer], None] | None = None,
) -> Episode:
    """Run one episode of ``task`` on ``board``.

    The start state and the wording are drawn from ``rng``, so a seed makes
    the episode repeatable. ``on_ready`` is called once the board is passive
    and the instruction is known; from then on whatever moves the modules
    (a policy, a hand, a simulated hand) is being scored. Ctrl+C ends the
    episode as ``aborted``.
    """
    rng = rng or random.Random()
    scorer = scorer or Scorer(task)
    start: dict[str, float] = {}
    if reset:
        start = task.sample_start(rng)
        board.reset(start, settings, clock)
    else:
        board.passive()
    instruction = task.sample_instruction(rng) if varied_wording else task.instruction
    if on_ready:
        on_ready(start, instruction)

    samples: list[Sample] = []
    origin = clock.now()
    interrupted = False
    try:
        while True:
            t = clock.now() - origin
            values = board.values()
            samples.append(Sample(round(t, 4), values))
            done = scorer.update(t, values)
            if on_sample:
                on_sample(t, values, scorer)
            if done:
                break
            clock.sleep(1.0 / hz)
    except KeyboardInterrupt:
        interrupted = True
        scorer.abort("operator stop")
    return Episode(task, instruction, start, scorer.result(), tuple(samples), interrupted)


class SimHand:
    """Stands in for the robot (or your hand) on the simulated board.

    It works on whichever step the scorer is on: it moves the goal modules of
    a reach step to their targets and keeps still during a hold. With
    ``skill`` below 1 it sometimes stops short of a target, which makes for a
    more lifelike spread of results.
    """

    def __init__(
        self,
        board: Board,
        scorer: Scorer,
        play: str = "solve",
        skill: float = 1.0,
        rng: random.Random | None = None,
        delay_s: float = 0.5,
        seconds_per_travel: float = 2.0,
    ):
        self.board = board
        self.scorer = scorer
        self.play = play
        self.delay_s = delay_s
        self.seconds_per_travel = seconds_per_travel
        self.started: float | None = None
        self._last: float | None = None
        rng = rng or random.Random()
        # Decide up front which steps this hand will fumble, so a seed fixes the episode.
        self._fumbles = [rng.random() >= skill for _ in scorer.task.steps]
        self._origins: dict[tuple[int, str], float] = {}

    def start(self, now: float) -> None:
        self.started = now

    def __call__(self, now: float) -> None:
        if self.started is None or self.play == "idle":
            return
        previous, self._last = self._last if self._last is not None else now, now
        if now - self.started < self.delay_s:
            return
        index = self.scorer.current_step
        step = self.scorer.steps[index]
        bus = self.board.bus
        for goal in step.goals:
            if goal.from_start:
                return  # the scorer has not seen its first sample yet
            spec = self.board.spec(goal.module)
            cal = self.board.calibration.modules[goal.module]
            target = goal.target if goal.target is not None else (max(goal.low, spec.work[0]) + min(goal.high, spec.work[1])) / 2
            position = bus.read_position(spec.servo_id)
            target_raw = spec.raw_from_value(target, cal.raw_lo, cal.raw_hi)
            if self._fumbles[index]:
                # Stop 40 % of the way short of the target.
                origin = self._origins.setdefault((index, goal.module), position)
                target_raw = origin + (target_raw - origin) * 0.6
            step_size = abs(cal.raw_hi - cal.raw_lo) / self.seconds_per_travel * (now - previous)
            bus.push(spec.servo_id, max(-step_size, min(step_size, target_raw - position)))
