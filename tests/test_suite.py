"""The standard test: its contents, its repeatability and its report."""

import io
import json

import pytest

from lebox import Board, Calibration, SimBus, SimClock, load_suite, run_suite, summarise
from lebox.cli import main
from lebox.modules import DEFAULT_MODULES
from lebox.suite import SuiteError, episode_seed, load_results, wilson_interval


def run(*argv):
    out = io.StringIO()
    return main(list(argv), out=out), out.getvalue()


def sim_board():
    calibration = Calibration.from_design()
    bus = SimBus.for_board(calibration)
    for spec in DEFAULT_MODULES:
        bus.write_torque_cap(spec.servo_id, 300)
    bus.power_cycle()
    return Board(bus, calibration), SimClock(bus)


def test_the_standard_test_is_what_was_agreed():
    suite = load_suite()
    assert (suite.name, suite.version, suite.episodes) == ("LeBox Standard Test", "1.0", 10)
    assert suite.levels == ("single", "short", "long", "control")
    assert suite.groups("single") == ("stops", "marks", "turns")
    assert suite.groups("long") == ("revisit", "timed")
    count = {level: sum(1 for item in suite.tasks if item.level == level) for level in suite.levels}
    assert count == {"single": 20, "short": 8, "long": 10, "control": 1}
    steps = {level: [len(item.task.steps) for item in suite.tasks if item.level == level] for level in suite.levels}
    assert set(steps["single"]) == {1} and set(steps["short"]) == {2, 3}
    assert min(steps["long"]) >= 3 and max(steps["long"]) == 6
    assert "C0 A2 D4 AS B0" in [item.code for item in suite.tasks]


def test_single_actions_cover_every_standard_state():
    suite = load_suite()
    targets = set()
    for item in suite.tasks:
        if item.level == "single":
            goal = item.task.steps[0].goals[0]
            targets.add(item.task.specs[goal.module].code_for_value(goal.target))
    assert len(targets) == 18


def test_every_long_revisit_task_uses_a_tile_twice():
    for item in load_suite().tasks:
        if (item.level, item.group) == ("long", "revisit"):
            modules = [goal.module for step in item.task.steps for goal in step.goals]
            assert len(modules) > len(set(modules)), item.code
        if (item.level, item.group) == ("long", "timed"):
            assert any(step.kind == "hold" for step in item.task.steps), item.code


def test_episode_seeds_depend_only_on_the_suite_the_task_and_the_number():
    assert episode_seed(2026, "D3", 0) == episode_seed(2026, "D3", 0)
    assert len({episode_seed(2026, code, n) for code in ("D3", "D4") for n in range(10)}) == 20
    assert episode_seed(2026, "D3", 0) != episode_seed(2027, "D3", 0)


def test_a_run_is_repeatable_and_a_perfect_hand_passes_everything(tmp_path):
    suite = load_suite()
    first = run_suite(suite, sim_board, tmp_path / "a", episodes=2, sim_play="solve")
    second = run_suite(suite, sim_board, tmp_path / "b", episodes=2, sim_play="solve")
    assert len(first) == 78
    assert [(r["code"], r["seed"], r["start"], r["instruction"]) for r in first] == [
        (r["code"], r["seed"], r["start"], r["instruction"]) for r in second
    ]
    assert all(record["result"]["success"] for record in first)
    # Two episodes of one task do not all begin in the same place.
    starts = [record["start"] for record in first if record["code"] == "D3"]
    assert len(starts) == 2
    assert (tmp_path / "a" / "results.jsonl").read_text().count("\n") == 78
    traces = sorted(path.name for path in (tmp_path / "a" / "traces").iterdir())
    assert len(traces) == 78  # one per episode: tasks that differ only in their start do not collide
    assert "single-turns-b0__b100-00.jsonl" in traces and "single-stops-b75__b100-00.jsonl" in traces
    assert "long-revisit-c0_a2_d4_as_b0-01.jsonl" in traces
    assert load_results(tmp_path / "a") == json.loads(json.dumps(first))


def test_plain_and_varied_wording(tmp_path):
    suite = load_suite()
    plain = run_suite(suite, sim_board, tmp_path / "p", episodes=4, levels=["single"], sim_play="solve", save_traces=False)
    varied = run_suite(suite, sim_board, tmp_path / "v", episodes=4, levels=["single"], sim_play="solve", varied_wording=True, save_traces=False)
    b0 = lambda records: {r["instruction"] for r in records if r["code"] == "B25 > B0"}
    assert b0(plain) == {"Set the blue valve to CLOSED"}
    assert len(b0(varied)) > 1 and b0(varied) <= {
        "Set the blue valve to CLOSED", "Close the blue valve", "Turn the blue valve all the way closed"
    }
    assert not (tmp_path / "p" / "traces").exists()
    # The wording changes; the task, the start state and the outcome do not.
    assert [(r["standard"], r["start"]) for r in plain] == [(r["standard"], r["start"]) for r in varied]


def test_an_idle_robot_fails_everything_except_the_restraint_task(tmp_path):
    records = run_suite(load_suite(), sim_board, tmp_path, episodes=1, sim_play="idle", save_traces=False)
    by_level = {row.level: row for row in summarise(records) if row.group == "all"}
    assert by_level["single"].successes == 0 and by_level["single"].outcomes == {"no_contact": 20}
    assert by_level["control"].successes == 1  # touching nothing is what that task asks for
    assert by_level["long"].steps_done == 0.0


def test_summary_counts_and_intervals(tmp_path):
    records = run_suite(load_suite(), sim_board, tmp_path, episodes=3, sim_play="solve", sim_skill=0.8, save_traces=False)
    rows = summarise(records)
    assert [(row.level, row.group) for row in rows] == [
        ("single", "stops"), ("single", "marks"), ("single", "turns"), ("single", "all"),
        ("short", "pairs"), ("short", "triples"), ("short", "all"),
        ("long", "revisit"), ("long", "timed"), ("long", "all"),
        ("control", "all"),
    ]
    single = rows[3]
    assert (single.tasks, single.episodes) == (20, 60)
    assert single.successes == sum(row.successes for row in rows[:3])
    assert single.low <= single.rate <= single.high
    assert sum(single.outcomes.values()) == 60
    long_all = rows[9]
    assert long_all.steps_total == pytest.approx(4.9) and long_all.steps_done < long_all.steps_total
    # An imperfect hand does worse the longer the task.
    assert rows[3].rate > rows[9].rate


def test_wilson_interval():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(8, 10)
    assert (low, high) == (pytest.approx(0.490, abs=0.005), pytest.approx(0.943, abs=0.005))
    assert wilson_interval(10, 10)[1] == 1.0 and wilson_interval(10, 10)[0] == pytest.approx(0.722, abs=0.005)
    assert wilson_interval(0, 10)[0] == 0.0


def test_suite_commands(tmp_path, capsys):
    code, text = run("suite", "list")
    assert code == 0 and text.startswith("LeBox Standard Test v1.0: 39 tasks, 10 episodes each\n")
    assert "single: one action" in text and "    C0 A2 D4 AS B0" in text

    out = tmp_path / "run"
    code, text = run("suite", "run", "--sim", "--out", str(out), "--episodes", "1", "--level", "short")
    assert code == 0
    lines = text.splitlines()
    assert lines[0] == "   1/8  short   C0 B50                  success"
    assert "LeBox Standard Test v1.0: 1 episode(s) per task, plain wording" in lines
    assert lines[-3].split() == ["short", "all", "8", "8", "8", "(100%)", "68%", "to", "100%", "2.5", "of", "2.5"]
    assert lines[-2] == "Outcomes: success 8" and lines[-1] == f"Results saved to {out / 'results.jsonl'}"

    code, text = run("suite", "report", str(out))
    assert code == 0 and "LeBox Standard Test v1.0: 8 episodes, plain wording" in text and "Outcomes: success 8" in text

    assert run("suite", "run", "--sim", "--out", str(out), "--level", "medium")[0] == 2
    assert "no level named medium" in capsys.readouterr().err
    assert run("suite", "report", str(tmp_path / "nothing"))[0] == 1
    assert "cannot read results" in capsys.readouterr().err


def test_codes_and_build_commands(tmp_path):
    code, text = run("codes")
    assert code == 0 and text.splitlines()[4] == "B0    Set the blue valve to CLOSED      Close the blue valve; Turn the blue valve all the way closed"
    code, text = run("task", "show", "B50 W30 D5")
    assert code == 0 and text.splitlines()[:2] == ["b50_w30_d5", "  code         B50 W30 D5"]
    output = tmp_path / "t.yaml"
    code, text = run("task", "build", "A1 > A2 D3 AS", "--name", "there_and_back", "-o", str(output))
    assert code == 0 and text.startswith(f"Wrote {output}: 3 step(s)\n")
    assert run("task", "check", str(output)) == (0, f"ok    {output}: there_and_back\n")
    code, text = run("task", "run", "--sim", "A1 > A2 D3 AS", "--seed", "1")
    assert code == 0 and "a2_d3_as: success" in text and "steps          3 of 3" in text


def test_a_broken_suite_file_is_reported(tmp_path):
    path = tmp_path / "suite.yaml"
    path.write_text("name: x\nversion: '1'\nepisodes: 1\nseed: 1\nlevels:\n  - id: a\n    groups:\n      - id: g\n        tasks: ['Z9']\n")
    with pytest.raises(SuiteError, match="task 'Z9' in a/g: 'Z9': there is no tile Z"):
        load_suite(path)
    path.write_text("name: x\n")
    with pytest.raises(SuiteError, match="is not a valid suite"):
        load_suite(path)
    with pytest.raises(SuiteError, match="cannot read suite file"):
        load_suite(tmp_path / "missing.yaml")
