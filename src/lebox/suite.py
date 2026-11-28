"""The standard test: a fixed set of tasks, run many times, reported by level.

A suite is a file of tasks written in step codes and grouped into levels.
The one that ships with LeBox is the LeBox Standard Test. It is frozen:
tasks, start states and wordings all follow from the file and its seed, so
two runs of the same version are the same test.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from .board import Board
from .codes import task_from_codes
from .episode import Episode, SimHand, run_episode
from .motion import Clock, MotionSettings, SimClock
from .scorer import Scorer
from .task import Task, TaskError
from .trace import save_trace


class SuiteError(ValueError):
    """The suite file is wrong."""


@dataclass(frozen=True)
class SuiteTask:
    level: str
    group: str
    code: str
    """The task as written in the suite file, in step codes."""
    task: Task


@dataclass(frozen=True)
class Suite:
    name: str
    version: str
    episodes: int
    seed: int
    tasks: tuple[SuiteTask, ...]
    titles: Mapping[str, str]
    """Titles of the levels (``single``) and groups (``single/stops``)."""

    @property
    def levels(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.level for item in self.tasks))

    def groups(self, level: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.group for item in self.tasks if item.level == level))


def load_suite(path: Path | str | None = None) -> Suite:
    """A suite from a file; with no path, the LeBox Standard Test that ships with LeBox."""
    if path is None:
        text = resources.files("lebox.library").joinpath("standard_test_v1.yaml").read_text(encoding="utf-8")
        where = "the built-in standard test"
    else:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise SuiteError(f"cannot read suite file {path}: {exc.strerror or exc}") from None
        where = str(path)
    try:
        data = yaml.safe_load(text)
        tasks: list[SuiteTask] = []
        titles: dict[str, str] = {}
        for level in data["levels"]:
            titles[level["id"]] = level.get("title", level["id"])
            for group in level["groups"]:
                titles[f"{level['id']}/{group['id']}"] = group.get("title", group["id"])
                for code in group["tasks"]:
                    try:
                        task = task_from_codes(str(code))
                    except TaskError as exc:
                        raise SuiteError(f"{where}: task '{code}' in {level['id']}/{group['id']}: {exc}") from None
                    tasks.append(SuiteTask(level["id"], group["id"], str(code), task))
        if not tasks:
            raise SuiteError(f"{where}: the suite has no tasks")
        return Suite(
            name=str(data["name"]),
            version=str(data["version"]),
            episodes=int(data["episodes"]),
            seed=int(data["seed"]),
            tasks=tuple(tasks),
            titles=titles,
        )
    except (yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SuiteError):
            raise
        raise SuiteError(f"{where} is not a valid suite: {exc!r}") from None


def episode_seed(suite_seed: int, code: str, episode: int) -> int:
    """The seed for one episode. It depends only on the suite seed, the task and the episode number."""
    digest = hashlib.sha256(f"{suite_seed}|{code}|{episode}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def run_suite(
    suite: Suite,
    make_board: Callable[[], tuple[Board, Clock]],
    out_dir: Path | str,
    episodes: int | None = None,
    levels: Iterable[str] | None = None,
    varied_wording: bool = False,
    settings: MotionSettings = MotionSettings(),
    sim_play: str | None = None,
    sim_skill: float = 1.0,
    hz: float = 20.0,
    save_traces: bool = True,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Run the suite and write ``results.jsonl`` (one line per episode) into ``out_dir``.

    ``make_board`` is called once and gives the board and its clock. With
    ``sim_play`` set, a simulated hand plays the robot. Returns the records.
    Ctrl+C stops the run after the present episode, keeping what was done.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    wanted = set(levels) if levels else None
    count = episodes or suite.episodes
    board, clock = make_board()
    records: list[dict] = []
    stopped = False
    with board, (out / "results.jsonl").open("w", encoding="utf-8") as results:
        for item in suite.tasks:
            if wanted is not None and item.level not in wanted:
                continue
            for number in range(count):
                seed = episode_seed(suite.seed, item.code, number)
                rng = random.Random(seed)
                scorer = Scorer(item.task)
                hand = None
                episode_clock = clock
                if sim_play is not None:
                    hand = SimHand(board, scorer, sim_play, skill=sim_skill, rng=random.Random(seed + 1))
                    episode_clock = SimClock(board.bus, on_tick=hand)
                episode: Episode = run_episode(
                    board,
                    item.task,
                    episode_clock,
                    settings,
                    rng,
                    varied_wording=varied_wording,
                    hz=hz,
                    scorer=scorer,
                    on_ready=(lambda start, instruction: hand.start(episode_clock.now())) if hand else None,
                )
                record = {
                    "suite": suite.name,
                    "version": suite.version,
                    "level": item.level,
                    "group": item.group,
                    "code": item.code,
                    "task": item.task.name,
                    "standard": item.task.signature,
                    "episode": number,
                    "seed": seed,
                    "wording": "varied" if varied_wording else "plain",
                    "instruction": episode.instruction,
                    "start": dict(episode.start),
                    "result": episode.result.to_dict(),
                }
                if save_traces:
                    trace = out / "traces" / f"{item.level}-{item.group}-{_slug(item.code)}-{number:02d}.jsonl"
                    save_trace(trace, episode.samples)
                    record["trace"] = str(trace.relative_to(out))
                results.write(json.dumps(record) + "\n")
                results.flush()
                records.append(record)
                if progress:
                    progress(record)
                if episode.interrupted:
                    stopped = True
                    break
            if stopped:
                break
    return records


def _slug(code: str) -> str:
    """A task's codes as a file name: 'B0 > B100' becomes 'b0__b100'."""
    return code.lower().replace(" > ", "__").replace(" ", "_").replace("+", "-").replace(".", "p")


def load_results(path: Path | str) -> list[dict]:
    source = Path(path)
    if source.is_dir():
        source = source / "results.jsonl"
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SuiteError(f"cannot read results {source}: {exc.strerror or exc}") from None
    try:
        records = [json.loads(line) for line in lines if line.strip()]
    except json.JSONDecodeError as exc:
        raise SuiteError(f"{source} is not a results file: {exc}") from None
    if not records:
        raise SuiteError(f"{source} holds no results")
    return records


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The 95 % Wilson score interval for a success rate."""
    if total == 0:
        return (0.0, 1.0)
    rate = successes / total
    centre = (rate + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class Row:
    level: str
    group: str
    """A group id, or ``all`` for the whole level."""
    tasks: int
    episodes: int
    successes: int
    low: float
    high: float
    steps_done: float
    steps_total: float
    outcomes: Mapping[str, int]

    @property
    def rate(self) -> float:
        return self.successes / self.episodes if self.episodes else 0.0


def summarise(records: Iterable[Mapping[str, Any]]) -> list[Row]:
    """Success rates by level and group, in the order the levels were run."""
    records = list(records)
    rows: list[Row] = []

    def row(level: str, group: str, chosen: list) -> Row:
        results = [record["result"] for record in chosen]
        successes = sum(1 for result in results if result["success"])
        low, high = wilson_interval(successes, len(results))
        return Row(
            level=level,
            group=group,
            tasks=len({record["code"] for record in chosen}),
            episodes=len(results),
            successes=successes,
            low=low,
            high=high,
            steps_done=sum(result["steps_completed"] for result in results) / len(results),
            steps_total=sum(result["steps_total"] for result in results) / len(results),
            outcomes=dict(Counter(result["outcome"] for result in results)),
        )

    for level in dict.fromkeys(record["level"] for record in records):
        in_level = [record for record in records if record["level"] == level]
        groups = list(dict.fromkeys(record["group"] for record in in_level))
        if len(groups) > 1:
            for group in groups:
                rows.append(row(level, group, [record for record in in_level if record["group"] == group]))
        rows.append(row(level, "all", in_level))
    return rows
