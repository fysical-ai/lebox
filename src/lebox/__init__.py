"""LeBox: a self-resetting, self-scoring test bench for robot manipulation policies."""

from .board import Board, MoveResult, Reading
from .bus import BusError, ServoBus, ServoStatus, SimBus
from .calibration import Calibration, CalibrationError, ModuleCalibration
from .codes import codes_for, task_from_codes
from .episode import Episode, run_episode
from .modules import DEFAULT_MODULES, Marking, ModuleError, ModuleSpec
from .motion import Clock, MotionError, MotionSettings, SimClock, StallError, drive
from .scorer import OUTCOMES, Result, ScoreError, Scorer, score
from .suite import Suite, load_suite, run_suite, summarise
from .task import Goal, Task, TaskError, builtin_names, find_task, load_builtin
from .trace import Sample, TraceError, load_trace, save_trace

__version__ = "0.0.1"

__all__ = [
    "Board",
    "BusError",
    "Calibration",
    "CalibrationError",
    "Clock",
    "Episode",
    "DEFAULT_MODULES",
    "Goal",
    "Marking",
    "ModuleCalibration",
    "ModuleError",
    "ModuleSpec",
    "MotionError",
    "MotionSettings",
    "MoveResult",
    "OUTCOMES",
    "Reading",
    "Result",
    "Sample",
    "ScoreError",
    "Scorer",
    "ServoBus",
    "ServoStatus",
    "SimBus",
    "SimClock",
    "StallError",
    "Suite",
    "Task",
    "TaskError",
    "TraceError",
    "builtin_names",
    "codes_for",
    "drive",
    "find_task",
    "load_builtin",
    "load_suite",
    "load_trace",
    "run_episode",
    "run_suite",
    "save_trace",
    "score",
    "summarise",
    "task_from_codes",
    "__version__",
]
