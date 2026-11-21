"""The ``lebox`` command."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Sequence, TextIO

from . import __version__
from .board import Board, Reading
from .bus import BusError, SimBus, demo_positions
from .calibration import Calibration, CalibrationError
from .modules import ModuleSpec
from .scorer import Result, ScoreError, score
from .task import Start, Task, TaskError, builtin_names, find_task, load_builtin
from .trace import TraceError, load_trace

BAR_WIDTH = 25

NO_DRIVER = (
    "lebox monitor cannot read a real board yet: the Feetech bus driver is the next piece,\n"
    "and it has to be written and tested against the hardware.\n"
    "Run 'lebox monitor --sim' to see the simulated board."
)


def format_value(reading: Reading) -> str:
    if reading.unit == "deg":
        return f"{reading.value:+.1f} deg"
    if reading.unit == "%":
        return f"{reading.value:.1f} %"
    return f"{reading.value:.2f}"


def position_bar(spec: ModuleSpec, value: float, width: int = BAR_WIDTH) -> str:
    """A text gauge from one hard stop to the other, with ``o`` at the element."""
    low, high = spec.stops
    fraction = min(1.0, max(0.0, (value - low) / (high - low)))
    cells = ["-"] * width
    cells[round(fraction * (width - 1))] = "o"
    return "|" + "".join(cells) + "|"


def render(board: Board, readings: dict[str, Reading], title: str) -> list[str]:
    lines = [title, f"{'module':<9}{'id':>3}{'raw':>7}{'value':>12}  {'at':<7} low stop to high stop"]
    for name, reading in readings.items():
        spec = board.spec(name)
        note = "" if reading.in_work_range else "  beyond working range"
        lines.append(
            f"{name:<9}{reading.servo_id:>3}{reading.raw:>7}{format_value(reading):>12}  "
            f"{reading.label or '-':<7} {position_bar(spec, reading.value)}{note}"
        )
    return lines


def _monitor(args: argparse.Namespace, out: TextIO) -> int:
    if not args.sim:
        print(NO_DRIVER, file=sys.stderr)
        return 2
    if args.hz <= 0:
        print("--hz must be greater than 0", file=sys.stderr)
        return 2
    if args.frames is not None and args.frames < 1:
        print("--frames must be 1 or more", file=sys.stderr)
        return 2

    calibration = Calibration.load(args.calibration) if args.calibration else Calibration.from_design()
    bus = SimBus.for_board(calibration)
    source = "design-value calibration" if calibration.nominal else f"calibration '{calibration.board_id}'"
    title = f"LeBox monitor: simulated board, {source}"
    frames = 1 if args.once else args.frames
    live = out.isatty() and frames != 1

    with Board(bus, calibration) as board:
        board.passive()
        start = time.monotonic()
        shown = 0
        previous_height = 0
        try:
            while frames is None or shown < frames:
                # --frames gives a repeatable sequence; a live view follows the clock.
                t = shown / args.hz if frames is not None else time.monotonic() - start
                for servo_id, raw in demo_positions(calibration, t).items():
                    bus.place(servo_id, raw)
                lines = render(board, board.read(), title)
                if live and previous_height:
                    out.write(f"\x1b[{previous_height}A")
                for line in lines:
                    out.write(("\x1b[2K" if live else "") + line + "\n")
                if live:
                    out.write("\x1b[2KCtrl+C to stop\n")
                out.flush()
                previous_height = len(lines) + 1
                shown += 1
                if frames is None or shown < frames:
                    time.sleep(1.0 / args.hz)
        except KeyboardInterrupt:
            out.write("\n")
    return 0


def _describe_start(start: Start) -> str:
    if start.kind == "fixed":
        return f"{start.values[0]:g}"
    if start.kind == "choice":
        return "one of " + ", ".join(f"{value:g}" for value in start.values)
    return f"anywhere from {start.values[0]:g} to {start.values[1]:g}"


def describe_task(task: Task) -> list[str]:
    lines = [task.name, f"  instruction  {task.instruction}"]
    label = "start"
    for module, start in task.start.items():
        lines.append(f"  {label:<11}  {module}: {_describe_start(start)}")
        label = ""
    label = "goal"
    for goal in task.goals:
        target = f" (target {goal.target:g})" if goal.target is not None else ""
        lines.append(f"  {label:<11}  {goal.module}: {goal.low:g} to {goal.high:g}{target}")
        label = ""
    others = task.others
    if task.others == "unchanged" and task.others_tol:
        others += " (" + ", ".join(f"{name} within {tol:g}" for name, tol in task.others_tol.items()) + ")"
    lines.append(f"  {'others':<11}  {others}")
    lines.append(f"  {'hold':<11}  {task.hold_s:g} s")
    lines.append(f"  {'timeout':<11}  {task.timeout_s:g} s")
    return lines


def describe_result(result: Result) -> list[str]:
    def row(label: str, text: str) -> str:
        return f"  {label:<13}  {text}"

    lines = [f"{result.task}: {result.outcome}"]
    if result.abort_reason:
        lines.append(row("reason", result.abort_reason))
    lines.append(row("duration", f"{result.duration_s:.1f} s"))
    if result.time_to_goal_s is not None:
        lines.append(row("time to goal", f"{result.time_to_goal_s:.1f} s"))
    lines.append(row("progress", f"{result.progress:.0%} (best {result.best_progress:.0%})"))
    lines.append(row("final error", ", ".join(f"{name} {error:.3g}" for name, error in result.final_error.items())))
    lines.append(row("moved", ", ".join(result.moved) or "-"))
    lines.append(row("disturbed", ", ".join(result.disturbed) or "-"))
    return lines


def _task_list(args: argparse.Namespace, out: TextIO) -> int:
    names = builtin_names()
    width = max(len(name) for name in names)
    for name in names:
        out.write(f"{name:<{width}}  {load_builtin(name).instruction}\n")
    return 0


def _task_show(args: argparse.Namespace, out: TextIO) -> int:
    out.write("\n".join(describe_task(find_task(args.task))) + "\n")
    return 0


def _task_check(args: argparse.Namespace, out: TextIO) -> int:
    failures = 0
    for path in args.files:
        try:
            task = Task.load(path)
        except TaskError as exc:
            failures += 1
            out.write(f"FAIL  {exc}\n")
        else:
            out.write(f"ok    {path}: {task.name}\n")
    return 1 if failures else 0


def _score(args: argparse.Namespace, out: TextIO) -> int:
    task = find_task(args.task)
    result = score(task, load_trace(args.trace), abort_reason=args.aborted)
    if args.json:
        out.write(json.dumps(result.to_dict(), indent=2) + "\n")
    else:
        out.write("\n".join(describe_result(result)) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lebox", description="Tools for the LeBox test bench.")
    parser.add_argument("--version", action="version", version=f"lebox {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    monitor = commands.add_parser(
        "monitor",
        help="show every module's position in real units",
        description="Show every module's position: encoder counts, scale value, marking and a gauge.",
    )
    monitor.add_argument("--sim", action="store_true", help="use the simulated board")
    monitor.add_argument("--calibration", metavar="FILE", help="calibration file (default with --sim: design values)")
    monitor.add_argument("--hz", type=float, default=10.0, help="refresh rate (default: 10)")
    count = monitor.add_mutually_exclusive_group()
    count.add_argument("--once", action="store_true", help="print one reading and exit")
    count.add_argument("--frames", type=int, metavar="N", help="print N readings and exit")
    monitor.set_defaults(run=_monitor)

    task = commands.add_parser("task", help="list, show and check tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True, metavar="action")
    task_list = task_commands.add_parser("list", help="list the built-in tasks")
    task_list.set_defaults(run=_task_list)
    task_show = task_commands.add_parser("show", help="show what a task asks for")
    task_show.add_argument("task", help="built-in task name or path to a task file")
    task_show.set_defaults(run=_task_show)
    task_check = task_commands.add_parser("check", help="check task files for mistakes")
    task_check.add_argument("files", nargs="+", metavar="FILE", help="task file(s) to check")
    task_check.set_defaults(run=_task_check)

    scoring = commands.add_parser(
        "score",
        help="score a recorded trace against a task",
        description="Score a recorded trace (JSON Lines, one board sample per line) against a task.",
    )
    scoring.add_argument("task", help="built-in task name or path to a task file")
    scoring.add_argument("trace", help="trace file")
    scoring.add_argument("--aborted", metavar="REASON", help="mark the episode as stopped from outside")
    scoring.add_argument("--json", action="store_true", help="print the result as JSON")
    scoring.set_defaults(run=_score)
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args, out or sys.stdout)
    except (CalibrationError, BusError, TaskError, TraceError, ScoreError) as exc:
        print(f"lebox: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # The reader went away, e.g. 'lebox task list | head -3'. Not an error worth a traceback.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
