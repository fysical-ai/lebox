"""The ``lebox`` command."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from . import __version__
from .board import Board, Reading
from .bus import BusError, ServoBus, SimBus, demo_positions
from .calibration import Calibration, CalibrationError, default_path
from .modules import DEFAULT_MODULES, ModuleError, ModuleSpec
from .motion import Clock, MotionError, MotionSettings, SimClock
from .scorer import Result, ScoreError, Scorer, score
from .task import Start, Task, TaskError, builtin_names, find_task, load_builtin
from .trace import Sample, TraceError, load_trace, save_trace

BAR_WIDTH = 25
SIM_CAP = 300

NO_BOARD = (
    "Which board? Give --port for a real one (for example --port /dev/ttyACM0; 'lebox ports' lists them)\n"
    "or --sim for the simulated board."
)


class UsageError(Exception):
    """The command line asks for something that cannot be done; exits with status 2."""


@dataclass
class Session:
    bus: ServoBus
    clock: Clock
    calibration: Calibration | None
    sim: bool
    label: str


@dataclass
class Hooks:
    """What the command line reaches the outside world through; tests replace these."""

    ask: Callable[[str], str] = input
    open_bus: Callable[[str], ServoBus] | None = None
    clock: Clock | None = None


def _open_real_bus(port: str) -> ServoBus:
    from .feetech import FeetechBus

    return FeetechBus(port)


def open_session(args: argparse.Namespace, hooks: Hooks, need_calibration: bool = True) -> Session:
    if getattr(args, "sim", False):
        calibration = Calibration.load(args.calibration) if args.calibration else Calibration.from_design()
        bus = SimBus.for_board(calibration)
        # The simulated board comes as 'lebox setup-motors' leaves a real one: torque caps stored.
        for spec in DEFAULT_MODULES:
            bus.write_torque_cap(spec.servo_id, SIM_CAP)
        bus.power_cycle()
        source = "design-value calibration" if calibration.nominal else f"calibration '{calibration.board_id}'"
        return Session(bus, SimClock(bus), calibration, True, f"simulated board, {source}")
    if not getattr(args, "port", None):
        raise UsageError(NO_BOARD)

    path = Path(args.calibration) if args.calibration else default_path(args.board_id)
    calibration = None
    if path.is_file():
        calibration = Calibration.load(path)
    elif need_calibration:
        raise CalibrationError(
            f"no calibration at {path}. For loose servos on the bench run "
            f"'lebox calibrate --bench --port {args.port}' first."
        )
    bus = (hooks.open_bus or _open_real_bus)(args.port)
    label = f"{args.port}, " + (f"calibration '{calibration.board_id}'" if calibration else "not calibrated")
    return Session(bus, hooks.clock or Clock(), calibration, False, label)


def motion_settings(args: argparse.Namespace) -> MotionSettings:
    return MotionSettings(torque=args.torque, max_cap=args.max_cap)


# Formatting


def format_value(value: float, unit: str) -> str:
    if unit == "deg":
        return f"{value:+.1f} deg"
    if unit == "%":
        return f"{value:.1f} %"
    return f"{value:.2f}"


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
            f"{name:<9}{reading.servo_id:>3}{reading.raw:>7}{format_value(reading.value, reading.unit):>12}  "
            f"{reading.label or '-':<7} {position_bar(spec, reading.value)}{note}"
        )
    return lines


def render_raw(bus: ServoBus, title: str) -> list[str]:
    lines = [title, f"{'module':<9}{'id':>3}{'raw':>7}"]
    for spec in DEFAULT_MODULES:
        raw = str(bus.read_position(spec.servo_id)) if bus.ping(spec.servo_id) else "no answer"
        lines.append(f"{spec.name:<9}{spec.servo_id:>3}{raw:>7}")
    return lines


class LiveView:
    """Redraws a block of lines in place on a terminal; prints plainly elsewhere."""

    def __init__(self, out: TextIO, live: bool):
        self.out = out
        self.live = live
        self._height = 0

    def show(self, lines: list[str], footer: str | None = None) -> None:
        if self.live and self._height:
            self.out.write(f"\x1b[{self._height}A")
        for line in lines:
            self.out.write(("\x1b[2K" if self.live else "") + line + "\n")
        self._height = len(lines)
        if self.live and footer:
            self.out.write("\x1b[2K" + footer + "\n")
            self._height += 1
        self.out.flush()


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


def _describe_moves(board: Board, results: dict) -> list[str]:
    lines = [f"{'module':<9}{'target':>12}{'reached':>12}{'error':>9}{'counts':>8}{'time':>8}"]
    for name, result in results.items():
        unit = board.spec(name).unit
        lines.append(
            f"{name:<9}{format_value(result.target, unit):>12}{format_value(result.reached, unit):>12}"
            f"{result.error:>+9.3f}{result.error_ticks:>+8d}{result.seconds:>6.1f} s"
        )
    return lines


# Commands: looking


def _ports(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    from .feetech import list_ports

    ports = list_ports()
    if not ports:
        out.write("No serial ports found. Is the servo adapter plugged in?\n")
        return 1
    for device, description in ports:
        out.write(f"{device}  {description}\n")
    return 0


def _ping(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    session = open_session(args, hooks, need_calibration=False)
    try:
        ids = args.id or list(range(1, args.scan_to + 1))
        found = [servo_id for servo_id in ids if session.bus.ping(servo_id)]
        if not found:
            out.write(f"No servo answered (tried id {ids[0]} to {ids[-1]}). Check power, cable and port.\n")
            return 1
        out.write(f"{'id':>3}{'model':>7}{'position':>10}{'volts':>7}{'temp':>6}  {'torque':<7}{'limit':>7}{'cap':>7}\n")
        uncapped = []
        for servo_id in found:
            status = session.bus.read_status(servo_id)
            out.write(
                f"{status.servo_id:>3}{status.model:>7}{status.position:>10}{status.voltage:>7.1f}"
                f"{status.temperature:>4} C  {'ON' if status.torque_enabled else 'off':<7}"
                f"{status.torque_limit / 10:>5.0f} %{status.torque_cap / 10:>5.0f} %\n"
            )
            if status.torque_cap > args.max_cap:
                uncapped.append(servo_id)
        if uncapped:
            ids_text = ", ".join(map(str, uncapped))
            out.write(f"No torque cap stored on id {ids_text}: 'lebox move' will refuse until 'lebox setup-motors' has run.\n")
        return 0
    finally:
        session.bus.close()


def _monitor(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    if args.hz <= 0:
        raise UsageError("--hz must be greater than 0")
    if args.frames is not None and args.frames < 1:
        raise UsageError("--frames must be 1 or more")
    session = open_session(args, hooks, need_calibration=False)
    frames = 1 if args.once else args.frames
    view = LiveView(out, live=out.isatty() and frames != 1)
    title = f"LeBox monitor: {session.label}"
    board = Board(session.bus, session.calibration) if session.calibration else None
    try:
        if board:
            board.passive()
        start = time.monotonic()
        shown = 0
        try:
            while frames is None or shown < frames:
                if session.sim:
                    # --frames gives a repeatable sequence; a live view follows the clock.
                    t = shown / args.hz if frames is not None else time.monotonic() - start
                    for servo_id, raw in demo_positions(session.calibration, t).items():
                        session.bus.place(servo_id, raw)
                lines = render(board, board.read(), title) if board else render_raw(session.bus, title)
                view.show(lines, footer="Ctrl+C to stop")
                shown += 1
                if frames is None or shown < frames:
                    time.sleep(1.0 / args.hz)
        except KeyboardInterrupt:
            out.write("\n")
    finally:
        session.bus.close()
    return 0


# Commands: setting up


def _setup_motors(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    if not 0 < args.cap <= args.max_cap:
        raise UsageError(f"--cap must be above 0 and at most {args.max_cap} per mille")
    session = open_session(args, hooks, need_calibration=False)
    if session.sim:
        session.bus.close()
        raise UsageError("setup-motors gives real servos their ids; the simulated board is already set up.")
    bus = session.bus
    try:
        out.write(
            "Each servo gets its id and a stored torque cap. Connect them ONE AT A TIME:\n"
            "new servos all share the same id, so two on the bus would both answer.\n\n"
        )
        for spec in DEFAULT_MODULES:
            while True:
                answer = hooks.ask(
                    f"Connect only the servo for the {spec.name} (it becomes id {spec.servo_id}), "
                    "then press Enter (s to skip, q to quit): "
                )
                choice = answer.strip().lower()
                if choice in ("s", "q"):
                    break
                found = [servo_id for servo_id in range(0, args.scan_to + 1) if bus.ping(servo_id)]
                if len(found) == 1:
                    break
                seen = "none" if not found else "ids " + ", ".join(map(str, found))
                out.write(f"  Expected exactly one servo on the bus, found {seen}. Check the wiring and try again.\n")
            if choice == "q":
                out.write("Stopped.\n")
                return 1
            if choice == "s":
                out.write(f"  {spec.name}: skipped\n")
                continue
            current = found[0]
            if current != spec.servo_id:
                bus.write_id(current, spec.servo_id)
            bus.write_torque_cap(spec.servo_id, args.cap)
            status = bus.read_status(spec.servo_id)
            renamed = f"id {current} -> {spec.servo_id}" if current != spec.servo_id else f"id {spec.servo_id}"
            out.write(
                f"  {spec.name}: {renamed}, torque cap {status.torque_cap / 10:g} %, "
                f"{status.voltage:.1f} V, {status.temperature} C, position {status.position}\n"
            )
        out.write(
            "\nDone. Switch the servo power off and on, connect all the servos, and run 'lebox ping':\n"
            "the limit column must show the cap, which proves the cap is what the servo starts with.\n"
        )
        return 0
    finally:
        bus.close()


def _calibrate(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    if not args.bench:
        raise UsageError(
            "Finding the hard stops needs the printed parts, and that routine is not written yet.\n"
            "For loose servos on the bench use: lebox calibrate --bench"
        )
    session = open_session(args, hooks, need_calibration=False)
    try:
        positions = {spec.name: session.bus.read_position(spec.servo_id) for spec in DEFAULT_MODULES}
        calibration = Calibration.from_bench(positions, board_id=args.board_id)
        if session.sim:
            path = "(not saved: simulated board)"
        else:
            path = calibration.save(args.calibration or None)
        out.write(f"Bench calibration for board '{args.board_id}': each horn's present position is mid-travel.\n")
        out.write(f"{'module':<9}{'id':>3}{'low end':>9}{'middle':>8}{'high end':>10}{'travel':>12}\n")
        for spec in DEFAULT_MODULES:
            entry = calibration.modules[spec.name]
            out.write(
                f"{spec.name:<9}{spec.servo_id:>3}{entry.raw_lo:>9}{positions[spec.name]:>8}{entry.raw_hi:>10}"
                f"{spec.servo_travel_deg:>8.1f} deg\n"
            )
        out.write(f"Saved to {path}\n")
        return 0
    finally:
        session.bus.close()


# Commands: moving


def _parse_targets(pairs: list[str]) -> dict[str, str]:
    targets = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name or not value:
            raise UsageError(f"'{pair}' is not a target; write module=position, for example slider=3 or switch=II")
        targets[name] = value
    return targets


def _known_modules(board: Board, names) -> None:
    known = [spec.name for spec in board.specs]
    unknown = [name for name in names if name not in known]
    if unknown:
        raise UsageError(f"no module named {', '.join(unknown)} (have: {', '.join(known)})")


def _move(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    targets = _parse_targets(args.targets)
    session = open_session(args, hooks)
    with Board(session.bus, session.calibration) as board:
        _known_modules(board, targets)
        results = board.move(targets, motion_settings(args), session.clock)
        out.write("\n".join(_describe_moves(board, results)) + "\n")
    return 0


def _reset(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    task = find_task(args.task)
    start = task.sample_start(random.Random(args.seed))
    session = open_session(args, hooks)
    with Board(session.bus, session.calibration) as board:
        results = board.reset(start, motion_settings(args), session.clock)
        out.write(f"{task.name}: start state in place, board passive.\n")
        out.write("\n".join(_describe_moves(board, results)) + "\n")
    return 0


def _selftest(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    if args.n < 1:
        raise UsageError("--n must be 1 or more")
    rng = random.Random(args.seed)
    session = open_session(args, hooks)
    settings = motion_settings(args)
    records = []
    with Board(session.bus, session.calibration) as board:
        modules = args.module or [spec.name for spec in board.specs]
        _known_modules(board, modules)
        for name in modules:
            spec = board.spec(name)
            for index in range(args.n):
                target = rng.uniform(*spec.work)
                result = board.move({name: target}, settings, session.clock)[name]
                records.append(
                    {"module": name, "target": target, "reached": result.reached, "error": result.error,
                     "error_ticks": result.error_ticks, "seconds": result.seconds}
                )
                if out.isatty():
                    out.write(f"\r{name} {index + 1}/{args.n}   ")
                    out.flush()
        if out.isatty():
            out.write("\r" + " " * 30 + "\r")
        out.write(f"Reset repeatability, {args.n} random targets per module (error after torque is released)\n")
        out.write(f"{'module':<9}{'mean |error|':>14}{'max |error|':>13}{'mean counts':>13}{'max counts':>12}{'mean time':>11}\n")
        for name in modules:
            rows = [record for record in records if record["module"] == name]
            errors = [abs(row["error"]) for row in rows]
            ticks = [abs(row["error_ticks"]) for row in rows]
            unit = board.spec(name).unit
            out.write(
                f"{name:<9}{format_value(statistics.mean(errors), unit).lstrip('+'):>14}"
                f"{format_value(max(errors), unit).lstrip('+'):>13}{statistics.mean(ticks):>13.1f}{max(ticks):>12d}"
                f"{statistics.mean(row['seconds'] for row in rows):>9.1f} s\n"
            )
    if args.json:
        Path(args.json).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        out.write(f"Every move saved to {args.json}\n")
    return 0


# Commands: tasks


def _task_list(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    names = builtin_names()
    width = max(len(name) for name in names)
    for name in names:
        out.write(f"{name:<{width}}  {load_builtin(name).instruction}\n")
    return 0


def _task_show(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    out.write("\n".join(describe_task(find_task(args.task))) + "\n")
    return 0


def _task_check(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
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


class SimHand:
    """Stands in for the robot (or your hand) on the simulated board."""

    def __init__(self, board: Board, task: Task, play: str, delay_s: float = 0.5, seconds_per_travel: float = 2.0):
        self.board = board
        self.task = task
        self.play = play
        self.delay_s = delay_s
        self.seconds_per_travel = seconds_per_travel
        self.started: float | None = None
        self._last: float | None = None

    def __call__(self, now: float) -> None:
        if self.started is None or self.play == "idle":
            return
        previous, self._last = self._last if self._last is not None else now, now
        if now - self.started < self.delay_s:
            return
        bus = self.board.bus
        for goal in self.task.goals:
            spec = self.board.spec(goal.module)
            cal = self.board.calibration.modules[goal.module]
            target = goal.target if goal.target is not None else (max(goal.low, spec.work[0]) + min(goal.high, spec.work[1])) / 2
            target_raw = spec.raw_from_value(target, cal.raw_lo, cal.raw_hi)
            position = bus.read_position(spec.servo_id)
            step = abs(cal.raw_hi - cal.raw_lo) / self.seconds_per_travel * (now - previous)
            bus.push(spec.servo_id, max(-step, min(step, target_raw - position)))


def _task_run(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    if args.hz <= 0:
        raise UsageError("--hz must be greater than 0")
    task = find_task(args.task)
    session = open_session(args, hooks)
    settings = motion_settings(args)
    with Board(session.bus, session.calibration) as board:
        hand = None
        if session.sim:
            hand = SimHand(board, task, args.sim_play)
            session.clock = SimClock(session.bus, on_tick=hand)
        if args.no_reset:
            board.passive()
            out.write(f"{task.name}: scoring from where the modules are now.\n")
        else:
            start = task.sample_start(random.Random(args.seed))
            board.reset(start, settings, session.clock)
            placed = ", ".join(f"{name} at {value:.3g}" for name, value in start.items())
            out.write(f"{task.name}: start state in place ({placed}). Board passive.\n")
        out.write(f'Instruction: "{task.instruction}"\n')

        scorer = Scorer(task)
        samples: list[Sample] = []
        view = LiveView(out, live=out.isatty())
        origin = session.clock.now()
        if hand:
            hand.started = origin
        interrupted = False
        try:
            while True:
                t = session.clock.now() - origin
                values = board.values()
                samples.append(Sample(round(t, 4), values))
                done = scorer.update(t, values)
                if view.live:
                    state = ", ".join(
                        f"{goal.module} {format_value(values[goal.module], board.spec(goal.module).unit)}"
                        for goal in task.goals
                    )
                    view.show([f"  {t:5.1f} s of {task.timeout_s:g} s   {state}   (Ctrl+C to stop)"])
                if done:
                    break
                session.clock.sleep(1.0 / args.hz)
        except KeyboardInterrupt:
            interrupted = True
            scorer.abort("operator stop")
        result = scorer.result()
        out.write("\n".join(describe_result(result)) + "\n")
        if args.trace:
            path = save_trace(args.trace, samples)
            out.write(f"Trace saved to {path}\n")
            if interrupted:
                out.write("Score it again later with: lebox score " f"{args.task} {path} --aborted 'operator stop'\n")
    return 0


def _score(args: argparse.Namespace, out: TextIO, hooks: Hooks) -> int:
    task = find_task(args.task)
    result = score(task, load_trace(args.trace), abort_reason=args.aborted)
    if args.json:
        out.write(json.dumps(result.to_dict(), indent=2) + "\n")
    else:
        out.write("\n".join(describe_result(result)) + "\n")
    return 0


# The parser


def _board_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("board")
    group.add_argument("--port", metavar="PORT", help="serial port of the servo adapter, e.g. /dev/ttyACM0")
    group.add_argument("--sim", action="store_true", help="use the simulated board")
    group.add_argument("--board-id", default="bench", metavar="NAME", help="which calibration to use (default: bench)")
    group.add_argument("--calibration", metavar="FILE", help="calibration file, instead of the one for --board-id")


def _motion_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("motion")
    group.add_argument("--torque", type=int, default=150, metavar="N", help="torque limit for moves, per mille (default: 150 = 15 %%)")
    group.add_argument("--max-cap", type=int, default=400, metavar="N", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lebox", description="Tools for the LeBox test bench.")
    parser.add_argument("--version", action="version", version=f"lebox {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    ports = commands.add_parser("ports", help="list serial ports")
    ports.set_defaults(run=_ports)

    ping = commands.add_parser("ping", help="find servos and show their state (read only)")
    _board_options(ping)
    ping.add_argument("--id", type=int, action="append", metavar="N", help="ask this id only (repeatable)")
    ping.add_argument("--scan-to", type=int, default=8, metavar="N", help="highest id to try (default: 8)")
    ping.add_argument("--max-cap", type=int, default=400, help=argparse.SUPPRESS)
    ping.set_defaults(run=_ping)

    monitor = commands.add_parser(
        "monitor",
        help="show every module's position in real units",
        description="Show every module's position: encoder counts, scale value, marking and a gauge.",
    )
    _board_options(monitor)
    monitor.add_argument("--hz", type=float, default=10.0, help="refresh rate (default: 10)")
    count = monitor.add_mutually_exclusive_group()
    count.add_argument("--once", action="store_true", help="print one reading and exit")
    count.add_argument("--frames", type=int, metavar="N", help="print N readings and exit")
    monitor.set_defaults(run=_monitor)

    setup = commands.add_parser("setup-motors", help="give each servo its id and a stored torque cap")
    _board_options(setup)
    setup.add_argument("--cap", type=int, default=300, metavar="N", help="torque cap to store, per mille (default: 300 = 30 %%)")
    setup.add_argument("--scan-to", type=int, default=8, metavar="N", help="highest id to look for (default: 8)")
    setup.add_argument("--max-cap", type=int, default=400, help=argparse.SUPPRESS)
    setup.set_defaults(run=_setup_motors)

    calibrate = commands.add_parser("calibrate", help="record each module's travel")
    _board_options(calibrate)
    calibrate.add_argument("--bench", action="store_true", help="loose servos without stops: the present position becomes mid-travel")
    calibrate.set_defaults(run=_calibrate)

    move = commands.add_parser("move", help="drive modules to positions, e.g. slider=3 switch=II")
    _board_options(move)
    _motion_options(move)
    move.add_argument("targets", nargs="+", metavar="module=position")
    move.set_defaults(run=_move)

    reset = commands.add_parser("reset", help="put a task's start state in place")
    _board_options(reset)
    _motion_options(reset)
    reset.add_argument("task", help="built-in task name or path to a task file")
    reset.add_argument("--seed", type=int, help="make the sampled start repeatable")
    reset.set_defaults(run=_reset)

    selftest = commands.add_parser("selftest", help="measure how repeatably modules reach random targets")
    _board_options(selftest)
    _motion_options(selftest)
    selftest.add_argument("--n", type=int, default=20, help="targets per module (default: 20)")
    selftest.add_argument("--module", action="append", metavar="NAME", help="test this module only (repeatable)")
    selftest.add_argument("--seed", type=int, default=0, help="seed for the random targets (default: 0)")
    selftest.add_argument("--json", metavar="FILE", help="save every move to this file")
    selftest.set_defaults(run=_selftest)

    task = commands.add_parser("task", help="list, show, check and run tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True, metavar="action")
    task_list = task_commands.add_parser("list", help="list the built-in tasks")
    task_list.set_defaults(run=_task_list)
    task_show = task_commands.add_parser("show", help="show what a task asks for")
    task_show.add_argument("task", help="built-in task name or path to a task file")
    task_show.set_defaults(run=_task_show)
    task_check = task_commands.add_parser("check", help="check task files for mistakes")
    task_check.add_argument("files", nargs="+", metavar="FILE", help="task file(s) to check")
    task_check.set_defaults(run=_task_check)
    task_run = task_commands.add_parser(
        "run",
        help="reset, then score an episode live",
        description="Put the task's start state in place, release the modules and score what happens next, live.",
    )
    _board_options(task_run)
    _motion_options(task_run)
    task_run.add_argument("task", help="built-in task name or path to a task file")
    task_run.add_argument("--seed", type=int, help="make the sampled start repeatable")
    task_run.add_argument("--no-reset", action="store_true", help="score from wherever the modules are now")
    task_run.add_argument("--trace", metavar="FILE", help="save the episode's trace here")
    task_run.add_argument("--hz", type=float, default=20.0, help="samples per second (default: 20)")
    task_run.add_argument("--sim-play", choices=["solve", "idle"], default="solve", help="what the simulated hand does (with --sim)")
    task_run.set_defaults(run=_task_run)

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


def main(argv: Sequence[str] | None = None, out: TextIO | None = None, hooks: Hooks | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args, out or sys.stdout, hooks or Hooks())
    except UsageError as exc:
        print(exc, file=sys.stderr)
        return 2
    except (CalibrationError, BusError, MotionError, ModuleError, TaskError, TraceError, ScoreError) as exc:
        print(f"lebox: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Moves release torque on the way out (see motion.drive), so stopping here is safe.
        print("\nlebox: stopped. Torque is released.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # The reader went away, e.g. 'lebox task list | head -3'. Not an error worth a traceback.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
