"""The ``lebox`` command."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Sequence, TextIO

from . import __version__
from .board import Board, Reading
from .bus import BusError, SimBus, demo_positions
from .calibration import Calibration, CalibrationError
from .modules import ModuleSpec

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
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args, out or sys.stdout)
    except (CalibrationError, BusError) as exc:
        print(f"lebox: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
