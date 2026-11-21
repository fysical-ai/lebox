"""LeBox: a self-resetting, self-scoring test bench for robot manipulation policies."""

from .board import Board, Reading
from .bus import BusError, ServoBus, SimBus
from .calibration import Calibration, CalibrationError, ModuleCalibration
from .modules import DEFAULT_MODULES, Marking, ModuleError, ModuleSpec

__version__ = "0.0.1"

__all__ = [
    "Board",
    "BusError",
    "Calibration",
    "CalibrationError",
    "DEFAULT_MODULES",
    "Marking",
    "ModuleCalibration",
    "ModuleError",
    "ModuleSpec",
    "Reading",
    "ServoBus",
    "SimBus",
    "__version__",
]
