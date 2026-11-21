import io
import subprocess
import sys

import pytest

from lebox.calibration import Calibration
from lebox.cli import main, position_bar
from lebox.modules import SLIDER


def run(*argv):
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


def test_monitor_once_shows_every_module_mid_range():
    code, text = run("monitor", "--sim", "--once")
    assert code == 0
    lines = text.splitlines()
    assert lines[0] == "LeBox monitor: simulated board, design-value calibration"
    assert lines[1].split()[:5] == ["module", "id", "raw", "value", "at"]
    rows = {line.split()[0]: line for line in lines[2:]}
    assert list(rows) == ["switch", "dial", "plunger", "slider"]
    assert "2048" in rows["switch"] and "+0.0 deg" in rows["switch"] and " O " in rows["switch"]
    assert "50.0 %" in rows["dial"]
    assert "2.00" in rows["plunger"]
    assert "3.00" in rows["slider"]
    assert "\x1b" not in text  # no cursor codes when not writing to a terminal


def test_monitor_frames_prints_a_repeatable_moving_sequence():
    code, first = run("monitor", "--sim", "--frames", "3", "--hz", "1000")
    assert code == 0
    assert first.count("LeBox monitor") == 3
    blocks = first.strip().split("LeBox monitor")[1:]
    assert blocks[0] != blocks[2]  # the demo motion moves the modules
    _, second = run("monitor", "--sim", "--frames", "3", "--hz", "1000")
    assert first == second


def test_monitor_uses_a_calibration_file(tmp_path):
    cal = Calibration.from_design(board_id="bench-1")
    cal.nominal = False
    path = cal.save(tmp_path / "bench-1.json")
    code, text = run("monitor", "--sim", "--once", "--calibration", str(path))
    assert code == 0
    assert "calibration 'bench-1'" in text


def test_monitor_reports_a_missing_calibration_file(tmp_path, capsys):
    code, _ = run("monitor", "--sim", "--once", "--calibration", str(tmp_path / "nope.json"))
    assert code == 1
    assert "no calibration file" in capsys.readouterr().err


def test_monitor_without_sim_says_the_driver_is_not_there_yet(capsys):
    code, text = run("monitor")
    assert code == 2
    assert text == ""
    assert "--sim" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["monitor", "--sim", "--hz", "0"], ["monitor", "--sim", "--frames", "0"]])
def test_monitor_rejects_bad_numbers(argv, capsys):
    code, _ = run(*argv)
    assert code == 2
    assert capsys.readouterr().err


def test_once_and_frames_cannot_be_combined():
    with pytest.raises(SystemExit):
        run("monitor", "--sim", "--once", "--frames", "2")


def test_position_bar_marks_the_ends_and_the_middle():
    low, high = SLIDER.stops
    assert position_bar(SLIDER, low, width=11) == "|o----------|"
    assert position_bar(SLIDER, (low + high) / 2, width=11) == "|-----o-----|"
    assert position_bar(SLIDER, high, width=11) == "|----------o|"
    assert position_bar(SLIDER, high + 5, width=11) == "|----------o|"  # clamped


def test_the_installed_command_runs():
    result = subprocess.run(
        [sys.executable, "-m", "lebox.cli", "monitor", "--sim", "--once"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "slider" in result.stdout
