import random

import pytest

from lebox import Task, TaskError, builtin_names, find_task, load_builtin
from lebox.modules import DEFAULT_MODULES

SPECS = {spec.name: spec for spec in DEFAULT_MODULES}

BASE = {
    "name": "slider_to_3",
    "instruction": "Move the slider to 3",
    "start": {"slider": {"sample": [1, 2, 4, 5]}, "dial": 50},
    "goal": {"slider": {"target": 3, "tol": 0.15}},
    "others": "unchanged",
    "hold_s": 1.0,
    "timeout_s": 30,
}


def make(**changes):
    data = {**BASE, **changes}
    return Task.from_dict({key: value for key, value in data.items() if value is not None})


def test_the_example_from_the_spec_loads():
    task = make()
    assert task.name == "slider_to_3"
    assert task.instruction == "Move the slider to 3"
    assert task.goal_modules == ("slider",)
    goal = task.goals[0]
    assert (goal.low, goal.high, goal.target) == (pytest.approx(2.85), pytest.approx(3.15), 3.0)
    assert task.start["slider"].kind == "choice"
    assert task.start["slider"].values == (1.0, 2.0, 4.0, 5.0)
    assert task.start["dial"].values == (50.0,)
    assert (task.others, task.hold_s, task.timeout_s) == ("unchanged", 1.0, 30.0)


def test_defaults():
    task = Task.from_dict(
        {"name": "t", "instruction": "Do it", "start": {"slider": 1}, "goal": {"slider": 3}}
    )
    assert (task.others, task.hold_s, task.timeout_s) == ("unchanged", 1.0, 30.0)
    # A bare goal value uses the module's own tolerance.
    assert task.goals[0].high - task.goals[0].low == pytest.approx(2 * SPECS["slider"].snap)


def test_builtin_tasks_all_load_and_cover_every_module():
    names = builtin_names()
    assert len(names) == 13
    covered = set()
    for name in names:
        task = load_builtin(name)
        assert task.name == name
        covered.update(task.goal_modules)
    assert covered == {"switch", "dial", "plunger", "slider"}


@pytest.mark.parametrize("name", builtin_names())
def test_builtin_starts_are_reachable_and_never_begin_solved(name):
    task = load_builtin(name)
    goals = {goal.module: goal for goal in task.goals}
    rng = random.Random(0)
    for _ in range(300):
        for module, value in task.sample_start(rng).items():
            assert SPECS[module].in_work_range(value)
            if module in goals:
                assert goals[module].distance(value) >= goals[module].margin - 1e-9


def test_sample_start_is_repeatable_with_a_seed():
    task = load_builtin("dial_open")
    assert task.sample_start(random.Random(5)) == task.sample_start(random.Random(5))
    assert task.sample_start(random.Random(5)) != task.sample_start(random.Random(6))


def test_start_forms():
    task = make(
        start={
            "slider": {"range": [4, 5]},
            "switch": "II",
            "dial": "random",
            "plunger": {"sample": [0, 4]},
        }
    )
    assert task.start["slider"] == task.start["slider"].__class__("range", (4.0, 5.0))
    assert task.start["switch"].values == (40.0,)
    assert task.start["dial"].kind == "range" and task.start["dial"].values == (0.0, 100.0)
    # Plunger rings 0 and 4 sit on the stops, so they resolve just inside.
    assert task.start["plunger"].values == (pytest.approx(0.1), pytest.approx(3.9))
    state = task.sample_start(random.Random(1))
    assert set(state) == {"slider", "switch", "dial", "plunger"}
    assert 4.0 <= state["slider"] <= 5.0


def test_a_range_spanning_the_goal_samples_both_sides_but_never_the_goal():
    task = make(start={"slider": {"range": [1, 5]}})
    rng = random.Random(3)
    values = [task.sample_start(rng)["slider"] for _ in range(500)]
    assert min(values) < 2.7 and max(values) > 3.3
    assert not any(2.7 < value < 3.3 for value in values)  # goal 2.85..3.15 plus its 0.15 margin


def test_goal_forms():
    assert make(goal={"slider": 3}).goals[0].target == 3.0
    low_side = make(start={"dial": 10}, goal={"dial": {"min": 75}}).goals[0]
    assert (low_side.low, low_side.high, low_side.target) == (75.0, SPECS["dial"].stops[1], None)
    high_side = make(start={"dial": 80}, goal={"dial": {"max": 25}}).goals[0]
    assert (high_side.low, high_side.high) == (SPECS["dial"].stops[0], 25.0)
    band = make(start={"dial": 10}, goal={"dial": {"min": 40, "max": 60}}).goals[0]
    assert (band.low, band.high) == (40.0, 60.0)
    label = make(start={"switch": "O"}, goal={"switch": "II"}).goals[0]
    assert (label.low, label.high) == (35.0, 45.0)


def test_several_goal_modules():
    task = make(start={"slider": 1, "switch": "O"}, goal={"slider": 3, "switch": "II"})
    assert task.goal_modules == ("slider", "switch")


def test_others_tol():
    task = make(others_tol={"dial": 10})
    assert task.others_tol == {"dial": 10.0}


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"name": None}, "'name' is required"),
        ({"name": "Slider To 3"}, "'name' is required"),
        ({"instruction": "  "}, "'instruction' must be a sentence"),
        ({"instruction": []}, "'instruction' must be a sentence"),
        ({"instruction": ["Move it", 3]}, "'instruction' must be a sentence"),
        ({"goal": None}, "'goal' is required"),
        ({"goal": {}}, "'goal' is required"),
        ({"goal": {"lever": 1}}, "unknown module 'lever'"),
        ({"start": {"slider": 1, "lever": 1}}, "unknown module 'lever'"),
        ({"goal": {"slider": 7}}, "outside the working range"),
        ({"goal": {"slider": "middle"}}, "neither a number nor one of the markings"),
        ({"goal": {"slider": {"target": 3, "tol": 0}}}, "'tol' must be greater than 0"),
        ({"goal": {"slider": {"target": 3, "tolerance": 0.1}}}, "only 'tol' may be added"),
        ({"goal": {"slider": {"near": 3}}}, "give a value"),
        ({"goal": {"slider": {}}}, "give a value"),
        ({"start": {"dial": 10}, "goal": {"dial": {"min": 60, "max": 40}}}, "must be below"),
        ({"start": {"dial": 10}, "goal": {"dial": {"min": 150}}}, "out of reach"),
        ({"start": {"slider": 3}}, "episode would begin solved"),
        ({"start": {"slider": {"sample": [1, 3.2]}}}, "episode would begin solved"),
        ({"start": {"slider": {"range": [2.9, 3.1]}}}, "whole range lies in or next to the goal"),
        ({"start": {"dial": 50}}, "every goal module needs a start"),
        ({"start": None}, "every goal module needs a start"),
        ({"start": {"slider": {"range": [5, 1]}}}, "must be below high"),
        ({"start": {"slider": {"range": [1]}}}, "'range' needs [low, high]"),
        ({"start": {"slider": {"sample": []}}}, "'sample' needs a list"),
        ({"start": {"slider": {"pick": [1]}}}, "give a value"),
        ({"start": {"slider": True}}, "expected a number or a marking"),
        ({"start": [1, 2]}, "'start' must map"),
        ({"others": "frozen"}, "'others' must be"),
        ({"others_tol": {"slider": 1}}, "this is a goal module"),
        ({"others_tol": {"dial": -1}}, "must be greater than 0"),
        ({"others_tol": {"lever": 1}}, "unknown module 'lever'"),
        ({"hold_s": -1}, "cannot be negative"),
        ({"hold_s": "long"}, "'hold_s' must be a number"),
        ({"timeout_s": 1.0}, "must be longer than 'hold_s'"),
        ({"timeout_s": True}, "'timeout_s' must be a number"),
        ({"tolerance": 1}, "unknown setting(s): tolerance"),
    ],
)
def test_mistakes_are_reported_clearly(changes, message):
    with pytest.raises(TaskError) as error:
        make(**changes)
    assert message in str(error.value)


def test_a_task_must_be_a_mapping():
    with pytest.raises(TaskError, match="must be a mapping"):
        Task.from_dict(["slider_to_3"])


def test_broken_yaml_is_a_task_error():
    with pytest.raises(TaskError, match="not valid YAML"):
        Task.from_yaml("name: [unclosed")


def test_load_names_the_file_in_errors(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: bad\ninstruction: Do it\ngoal: {slider: 9}\n", encoding="utf-8")
    with pytest.raises(TaskError) as error:
        Task.load(path)
    assert str(path) in str(error.value) and "outside the working range" in str(error.value)
    with pytest.raises(TaskError, match="cannot read task file"):
        Task.load(tmp_path / "missing.yaml")


def test_find_task_takes_a_builtin_name_or_a_file(tmp_path):
    assert find_task("slider_to_3").name == "slider_to_3"
    path = tmp_path / "mine.yaml"
    path.write_text(
        "name: mine\ninstruction: Open it\nstart: {dial: 10}\ngoal: {dial: {min: 75}}\n", encoding="utf-8"
    )
    assert find_task(str(path)).name == "mine"
    with pytest.raises(TaskError, match="neither a built-in task nor a task file"):
        find_task("does_not_exist")
    with pytest.raises(TaskError, match="no built-in task"):
        load_builtin("does_not_exist")


def test_the_task_keeps_its_source_for_storing_with_results():
    assert make().source["goal"] == {"slider": {"target": 3, "tol": 0.15}}
