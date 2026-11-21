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


# Tasks and scoring


def write_trace(tmp_path, moves, duration=5.0):
    from conftest import Episode
    from lebox import save_trace

    return save_trace(tmp_path / "ep.jsonl", Episode().samples(moves, duration=duration))


def test_task_list_shows_every_builtin_with_its_instruction():
    code, text = run("task", "list")
    assert code == 0
    lines = text.splitlines()
    assert len(lines) == 11
    assert "slider_to_3   Move the slider to 3" in lines
    assert "dial_open     Open the valve fully" in lines


def test_task_show_describes_a_builtin():
    code, text = run("task", "show", "slider_to_3")
    assert code == 0
    assert text.splitlines() == [
        "slider_to_3",
        "  instruction  Move the slider to 3",
        "  start        slider: one of 1, 2, 4, 5",
        "  goal         slider: 2.85 to 3.15 (target 3)",
        "  others       unchanged",
        "  hold         1 s",
        "  timeout      30 s",
    ]


def test_task_show_reads_a_file_and_reports_an_unknown_name(tmp_path, capsys):
    path = tmp_path / "mine.yaml"
    path.write_text(
        "name: mine\ninstruction: Open it\nstart: {dial: {range: [0, 50]}, slider: 2}\n"
        "goal: {dial: {min: 75}}\nothers_tol: {slider: 0.5}\n",
        encoding="utf-8",
    )
    code, text = run("task", "show", str(path))
    assert code == 0
    assert "  start        dial: anywhere from 0 to 50" in text
    assert "               slider: 2" in text
    assert "  goal         dial: 75 to 100.741" in text
    assert "  others       unchanged (slider within 0.5)" in text
    code, _ = run("task", "show", "nope")
    assert code == 1 and "neither a built-in task nor a task file" in capsys.readouterr().err


def test_task_check_passes_good_files_and_fails_bad_ones(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text("name: good\ninstruction: Do it\nstart: {slider: 1}\ngoal: {slider: 3}\n", encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\ninstruction: Do it\nstart: {slider: 3}\ngoal: {slider: 3}\n", encoding="utf-8")
    code, text = run("task", "check", str(good))
    assert code == 0 and text == f"ok    {good}: good\n"
    code, text = run("task", "check", str(good), str(bad), str(tmp_path / "missing.yaml"))
    assert code == 1
    lines = text.splitlines()
    assert lines[0].startswith("ok ") and lines[1].startswith("FAIL ") and lines[2].startswith("FAIL ")
    assert "episode would begin solved" in lines[1] and "cannot read task file" in lines[2]


def test_score_prints_the_outcome(tmp_path):
    trace = write_trace(tmp_path, [(0.0, 2.0, "slider", 3.0)])
    code, text = run("score", "slider_to_3", str(trace))
    assert code == 0
    assert text.splitlines() == [
        "slider_to_3: success",
        "  duration       2.9 s",
        "  time to goal   1.9 s",
        "  progress       100% (best 100%)",
        "  final error    slider 0",
        "  moved          slider",
        "  disturbed      -",
    ]


def test_score_json_and_aborted(tmp_path):
    import json

    trace = write_trace(tmp_path, [(0.0, 2.0, "slider", 2.0)], duration=1.0)
    code, text = run("score", "slider_to_3", str(trace), "--json", "--aborted", "operator stop")
    assert code == 0
    data = json.loads(text)
    assert data["outcome"] == "aborted" and data["abort_reason"] == "operator stop"
    code, text = run("score", "slider_to_3", str(trace), "--aborted", "operator stop")
    assert "slider_to_3: aborted" in text and "  reason         operator stop" in text


def test_score_reports_bad_input(tmp_path, capsys):
    code, _ = run("score", "slider_to_3", str(tmp_path / "missing.jsonl"))
    assert code == 1 and "cannot read trace file" in capsys.readouterr().err
    partial = tmp_path / "partial.jsonl"
    partial.write_text('{"t": 0, "values": {"slider": 1}}\n')
    code, _ = run("score", "slider_to_3", str(partial))
    assert code == 1 and "has no value for" in capsys.readouterr().err
