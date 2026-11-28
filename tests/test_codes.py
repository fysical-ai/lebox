"""Step codes: a whole task on one line."""

import random

import pytest

from lebox import TaskError, codes_for, find_task, load_builtin, task_from_codes
from lebox.codes import codes_to_dict, looks_like_codes, standard_steps
from lebox.modules import DEFAULT_MODULES, DIAL, PLUNGER, SLIDER, SWITCH


def test_the_code_number_is_the_number_printed_on_the_tile():
    assert [spec.letter for spec in DEFAULT_MODULES] == ["A", "B", "C", "D"]
    # The switch's printed O, I, II read as 0, 1, 2.
    assert [SWITCH.value_for_code(n) for n in (0, 1, 2)] == [0.0, -40.0, 40.0]
    assert SWITCH.standard_codes == (0.0, 1.0, 2.0)
    assert DIAL.standard_codes == (0.0, 25.0, 50.0, 75.0, 100.0) and DIAL.value_for_code(50) == 50
    assert PLUNGER.standard_codes == (0.0, 1.0, 2.0, 3.0, 4.0)
    assert SLIDER.standard_codes == (1.0, 2.0, 3.0, 4.0, 5.0)
    assert sum(len(spec.standard_codes) for spec in DEFAULT_MODULES) == 18


def test_codes_and_values_convert_both_ways():
    assert SWITCH.code_for_value(-40.0) == "A1" and SWITCH.code_for_value(40.0) == "A2"
    assert SWITCH.code_for_value(20.0) is None  # the switch has no in-between codes
    assert DIAL.code_for_value(50.0) == "B50" and DIAL.code_for_value(37.0) == "B37"
    assert PLUNGER.code_for_value(0.1) == "C0"  # ring 0 sits on the stop; the target is just inside
    assert SLIDER.code_for_value(2.5) == "D2.5"


def test_the_users_example_on_one_line():
    task = task_from_codes("A1 B100 C4 D1 > C0 A2 D4 AS B0")
    assert task.name == "c0_a2_d4_as_b0"
    assert [step.kind for step in task.steps] == ["reach"] * 5
    assert task.signature == (
        "plunger=0.1±0.15 within 20s | switch=40±5 within 20s | slider=4±0.15 within 20s "
        "| switch=start±5 within 20s | dial=0±3 within 20s"
    )
    assert task.sample_start(random.Random(0)) == {"switch": -40.0, "dial": 100.0, "plunger": 3.9, "slider": 1.0}
    assert task.instruction == (
        "Set the red plunger to 0, then set the black switch to II, then set the white slider to 4, "
        "then put the black switch back where it started, then set the blue valve to CLOSED."
    )
    assert codes_for(task) == "C0 A2 D4 AS B0"


def test_waits_and_joint_steps():
    task = task_from_codes("b50 w30 d5+a2")  # lower case is fine
    reach, hold, both = task.steps
    assert (hold.kind, hold.hold_s, hold.early_s) == ("hold", 30.0, 3.0)  # 10 % may be skipped
    assert [goal.module for goal in both.goals] == ["slider", "switch"]
    assert task.signature == "dial=50±3 within 20s | hold 30s (-3s) | slider=5±0.15 & switch=40±5 within 20s"
    assert codes_for(task) == "B50 W30 D5+A2" and task.name == "b50_w30_d5-a2"
    assert task_from_codes("W5").steps[0].early_s == 0.5
    assert task_from_codes("B50, W30, D5").signature == task_from_codes("B50 W30 D5").signature  # commas allowed


def test_a_tile_without_a_start_begins_at_one_of_its_other_standard_states():
    task = task_from_codes("B50 D5 A0")
    assert task.start["dial"].values == (0.0, 25.0, 75.0, 100.0)
    assert task.start["slider"].values == (1.0, 2.0, 3.0, 4.0)
    assert task.start["switch"].values == (-40.0, 40.0)
    assert "plunger" not in task.start  # not used, so left where it is
    partly = task_from_codes("B0 > B50 D5")
    assert partly.start["dial"].values == (0.0,) and partly.start["slider"].kind == "choice"


def test_in_between_values_are_legal_but_have_no_library_wording():
    task = task_from_codes("B37 D2.5")
    assert task.signature == "dial=37±3 within 20s | slider=2.5±0.15 within 20s"
    assert task.instruction == "Set the blue valve to 37%, then set the white slider to 2.5."
    assert task.steps[0].say == () and task.name == "b37_d2p5"
    assert codes_for(task) == "B37 D2.5"


def test_standard_steps_come_with_a_plain_sentence_and_alternatives():
    rows = standard_steps()
    assert [code for code, _, _ in rows] == [
        "A0", "A1", "A2", "B0", "B25", "B50", "B75", "B100", "C0", "C1", "C2", "C3", "C4", "D1", "D2", "D3", "D4", "D5",
    ]
    assert all(plain and len(others) == 2 for _, plain, others in rows)
    for code, plain, _ in rows:
        # The plain sentence is exactly what the task would generate with no library.
        assert task_from_codes(code).plain_wording(task_from_codes(code).steps[0]) == plain
    task = task_from_codes("B0")
    assert task.steps[0].say == ("Set the blue valve to CLOSED", "Close the blue valve", "Turn the blue valve all the way closed")
    assert {task.sample_instruction(random.Random(seed)) for seed in range(30)} == set(task.steps[0].say)
    back = task_from_codes("A2 D3 AS").steps[2].say
    assert back[0] == "Put the black switch back where it started" and len(back) == 3
    assert task_from_codes("W30").steps[0].say == ("Wait 30 seconds", "Pause for 30 seconds", "Hold still for 30 seconds")


def test_codes_for_only_speaks_for_standard_steps():
    assert codes_for(load_builtin("slider_to_3")) is None  # no time limit: not what D3 means
    assert codes_for(load_builtin("valve_wait_slider")) is None  # its wait allows 5 s early, not 3
    assert codes_for(task_from_codes("D3")) == "D3"


def test_a_line_of_codes_works_wherever_a_task_name_does():
    assert find_task("B50 W30 D5").name == "b50_w30_d5"
    assert find_task("slider_to_3").name == "slider_to_3"  # built-in names still win
    assert looks_like_codes("B50 W30 D5") and looks_like_codes("A1 > A2") and looks_like_codes("b50+d5")
    assert not looks_like_codes("slider_to_3") and not looks_like_codes("my task.yaml") and not looks_like_codes("")


def test_codes_to_dict_gives_a_task_file_mapping():
    data = codes_to_dict("B0 > B50 W10", name="half")
    assert data["name"] == "half" and data["start"] == {"dial": 0}
    assert data["steps"][0]["reach"] == {"dial": 50} and data["steps"][0]["within_s"] == 20
    assert data["steps"][1]["hold_s"] == 10 and data["steps"][1]["early_s"] == 1


@pytest.mark.parametrize(
    "line, message",
    [
        ("A3", "A3 is not a state of the switch; use A0, A1, A2"),
        ("E1", "there is no tile E (tiles: A, B, C, D)"),
        ("B150", "outside the working range"),
        ("D0", "outside the working range"),
        ("AS", "only makes sense after an earlier step"),
        ("A1 > A1", "episode would begin solved"),
        ("B50 > > D1", "at most one '>'"),
        ("", "no steps"),
        ("B0 >", "no steps"),
        ("W0", "a wait must be longer than 0 seconds"),
        ("B50+B25", "tile B appears twice in one step"),
        ("AS > D1", "cannot be part of a start state"),
        ("B0 B0 > D1", "the start state gives tile B twice"),
        ("D3 D3", "step 1: slider would already be in this goal"),
        ("B5O", "is not a step code"),
        ("valve", "is not a step code"),
    ],
)
def test_mistakes_in_codes_are_explained(line, message):
    with pytest.raises(TaskError) as error:
        task_from_codes(line)
    assert message in str(error.value)
