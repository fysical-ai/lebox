"""LeBox: a self-resetting, self-scoring test bench for robot manipulation policies."""

from .board import Board, Reading
from .bus import BusError, ServoBus, SimBus
from .calibration import Calibration, CalibrationError, ModuleCalibration
from .modules import DEFAULT_MODULES, Marking, ModuleError, ModuleSpec
from .scorer import OUTCOMES, Result, ScoreError, Scorer, score
from .task import Goal, Task, TaskError, builtin_names, find_task, load_builtin
from .trace import Sample, TraceError, load_trace, save_trace

__version__ = "0.0.1"

__all__ = [
    "Board",
    "BusError",
    "Calibration",
    "CalibrationError",
    "DEFAULT_MODULES",
    "Goal",
    "Marking",
    "ModuleCalibration",
    "ModuleError",
    "ModuleSpec",
    "OUTCOMES",
    "Reading",
    "Result",
    "Sample",
    "ScoreError",
    "Scorer",
    "ServoBus",
    "SimBus",
    "Task",
    "TaskError",
    "TraceError",
    "builtin_names",
    "find_task",
    "load_builtin",
    "load_trace",
    "save_trace",
    "score",
    "__version__",
]
