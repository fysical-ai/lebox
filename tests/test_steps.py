"""Tasks with several steps: the format, the wording and how they are scored."""

import json
import random

import pytest

from lebox import OUTCOMES, Task, TaskError, load_builtin, score
from lebox.task import table_to_dict

START = {"dial": 10, "slider": 1}

VALVE_WAIT_SLIDER = {
    "name": "valve_wait_slider",
    "start": START,
    "steps": [
        {"say": ["Turn the blue valve to 50%", "Set the blue valve half open"], "reach": {"dial": 50}, "within_s": 20},
        {"say": "Wait 30 seconds", "hold_s": 30, "early_s": 5},
        {"say": "Slide the slider to 5", "reach": {"slider": 5}, "within_s": 20},
    ],
}


def make(steps=None, **changes):
    data = {**VALVE_WAIT_SLIDER, **changes}
    if steps is not None:
        data["steps"] = steps
    return Task.from_dict(data)


TASK = make()
DIAL_THEN_SLIDER = make(steps=[{"reach": {"dial": 50}}, {"reach": {"slider": 5}}])
DIAL_OPEN = [(0.0, 2.0, "dial", 50.0)]  # crosses 47 at 1.85 s: first sample in the goal is 1.9 s


def play(episode, task, moves, duration=80.0, start=None):
    return episode(start={"dial": 10.0, "slider": 1.0, **(start or {})}).play(task, moves, duration=duration)


# The format


def test_a_task_with_steps_loads():
    assert [step.kind for step in TASK.steps] == ["reach", "hold", "reach"]
    reach, hold, last = TASK.steps
    assert (reach.goals[0].module, reach.goals[0].target, reach.within_s) == ("dial", 50.0, 20.0)
    assert (hold.hold_s, hold.early_s, hold.goals) == (30.0, 5.0, ())
    assert last.goals[0].module == "slider"
    assert TASK.goal_modules == ("dial", "slider")
    assert TASK.goals == reach.goals  # the first reach step


def test_default_timeout_covers_every_step_and_its_confirmation():
    # 20 + 30 + 20 for the steps, 1 s final hold, 0.5 s to confirm each of the two earlier steps.
    assert TASK.timeout_s == pytest.approx(72.0)
    assert DIAL_THEN_SLIDER.timeout_s == pytest.approx(30 + 30 + 1 + 0.5)
    assert make(timeout_s=120).timeout_s == 120.0


def test_the_standard_description_is_generated_from_the_steps():
    assert TASK.signature == "dial=50±3 within 20s | hold 30s (-5s) | slider=5±0.15 within 20s"
    reworded = make(steps=[{**step, "say": "Something else entirely"} for step in VALVE_WAIT_SLIDER["steps"]])
    assert reworded.signature == TASK.signature  # wording does not change what the task is
    assert load_builtin("slider_to_3").signature == "slider=3±0.15"
    other = make(start={"dial": 10, "switch": "I"}, steps=[{"reach": {"dial": {"min": 75}}}, {"reach": {"switch": "II"}}, {"reach": {"switch": "start"}}])
    assert other.signature == "dial=75..100.741 | switch=40±5 | switch=start±5"


def test_the_instruction_is_the_whole_sequence():
    assert TASK.instruction == "Turn the blue valve to 50%, then wait 30 seconds, then slide the slider to 5."
    wordings = {TASK.sample_instruction(random.Random(seed)) for seed in range(40)}
    assert wordings == {
        "Turn the blue valve to 50%, then wait 30 seconds, then slide the slider to 5.",
        "Set the blue valve half open, then wait 30 seconds, then slide the slider to 5.",
    }
    assert TASK.sample_instruction(random.Random(3)) == TASK.sample_instruction(random.Random(3))


def test_a_whole_task_wording_takes_precedence_over_the_steps():
    task = make(instruction=["Half open the valve, give it thirty seconds, then slider to 5", "Valve 50, wait, slider 5"])
    assert task.instruction == "Half open the valve, give it thirty seconds, then slider to 5"
    assert {task.sample_instruction(random.Random(seed)) for seed in range(20)} == set(task.instructions)


def test_steps_without_wording_get_a_plain_generated_sentence():
    task = load_builtin("five_step_tour")
    assert task.instruction == (
        "Set the red plunger to 0, then set the black switch to II, then set the white slider to 4, "
        "then put the black switch back where it started, then set the blue valve to CLOSED."
    )
    varied = make(
        start={"dial": 10, "slider": 1, "switch": "O"},
        steps=[
            {"reach": {"dial": {"min": 75}, "slider": 3}},
            {"hold_s": 1},
            {"reach": {"dial": {"max": 25}}},
            {"reach": {"dial": {"min": 40, "max": 60}, "switch": 20}},
        ],
    )
    assert varied.instruction == (
        "Set the blue valve to at least 75% and the white slider to 3, then wait 1 second, "
        "then set the blue valve to at most 25%, then set the blue valve to between 40% and 60% "
        "and the black switch to 20 degrees."
    )


def test_a_one_step_task_can_have_several_wordings_and_no_wording_at_all():
    task = Task.from_dict(
        {"name": "close", "instruction": ["Close the blue valve", "Turn the blue dial to 0%"], "start": {"dial": 60}, "goal": {"dial": 0}}
    )
    assert task.instruction == "Close the blue valve" and len(task.instructions) == 2
    bare = Task.from_dict({"name": "close", "start": {"dial": 60}, "goal": {"dial": 0}})
    assert bare.instruction == "Set the blue valve to CLOSED"
    assert bare.signature == task.signature == "dial=0±3"


def test_start_as_a_target_means_where_the_module_began():
    task = make(start={"switch": "I", "slider": 1}, steps=[{"reach": {"switch": "II"}}, {"reach": {"switch": {"target": "start", "tol": 2}}}])
    back = task.steps[1].goals[0]
    assert back.from_start and (back.low, back.high) == (-2.0, 2.0)
    fixed = back.resolved(-40.0)
    assert (fixed.low, fixed.high, fixed.target, fixed.from_start) == (-42.0, -38.0, -40.0, False)
    with pytest.raises(TaskError, match="relative to the start"):
        back.satisfied(0.0)


def test_starts_are_kept_clear_of_the_first_goal_for_each_module():
    task = make(start={"dial": {"range": [0, 100]}, "slider": {"range": [1, 5]}})
    rng = random.Random(0)
    for _ in range(300):
        state = task.sample_start(rng)
        assert abs(state["dial"] - 50) >= 6 - 1e-9  # goal 47..53 plus its 3 % margin
        assert state["slider"] <= 4.7 + 1e-9


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"goal": {"dial": 50}}, "either 'goal' (one step) or 'steps', not both"),
        ({"steps": []}, "'steps' must be a list with at least one step"),
        ({"steps": {"reach": {"dial": 50}}}, "'steps' must be a list"),
        ({"steps": ["reach dial 50"]}, "step 0: a step is a mapping"),
        ({"steps": [{"say": "Do it"}]}, "step 0: give exactly one of 'reach'"),
        ({"steps": [{"reach": {"dial": 50}, "hold_s": 5}]}, "step 0: give exactly one of 'reach'"),
        ({"steps": [{"reach": {"dial": 50}, "wait": 5}]}, "step 0: unknown setting(s): wait"),
        ({"steps": [{"reach": {"dial": 50}, "within_s": 0}]}, "'within_s' must be greater than 0"),
        ({"steps": [{"reach": {"dial": 50}, "early_s": 1}]}, "'early_s' belongs to a hold step"),
        ({"steps": [{"hold_s": 0}]}, "'hold_s' must be greater than 0"),
        ({"steps": [{"hold_s": 10, "early_s": 10}]}, "'early_s' must be 0 or more and less than 'hold_s'"),
        ({"steps": [{"hold_s": 10, "within_s": 5}]}, "'within_s' belongs to a reach step"),
        ({"steps": [{"reach": {}}]}, "'step 0 reach' is required"),
        ({"steps": [{"reach": {"lever": 1}}]}, "unknown module 'lever'"),
        ({"steps": [{"reach": {"dial": 50}, "say": []}]}, "'step 0 say' must be a sentence"),
        ({"steps": [{"reach": {"dial": "start"}}]}, "step 0: 'dial: start' only makes sense after an earlier step"),
        ({"steps": [{"reach": {"dial": 50}}, {"reach": {"dial": 51}}]}, "step 1: dial would already be in this goal"),
        ({"steps": [{"reach": {"dial": 50}}, {"reach": {"switch": "II"}}]}, "every goal module needs a start"),
        ({"start": {"dial": 10, "slider": 5}}, "start slider: 5 is in or next to the goal"),
        ({"timeout_s": 25}, "leaves no time after the hold steps"),
        ({"settle_s": -1}, "'settle_s' cannot be negative"),
    ],
)
def test_mistakes_in_steps_are_reported_with_the_step(changes, message):
    with pytest.raises(TaskError) as error:
        make(**changes)
    assert message in str(error.value)


def test_going_back_to_a_start_that_is_already_the_present_goal_is_rejected():
    # Step 0 moves the switch from -40 to -28 +- 2; "back to the start, give or take 12" is then already true.
    steps = [{"reach": {"switch": {"target": -28, "tol": 2}}}, {"reach": {"switch": {"target": "start", "tol": 12}}}]
    with pytest.raises(TaskError, match="step 1: switch would already be in this goal"):
        make(start={"switch": "I", "slider": 1}, steps=steps)
    steps[1]["reach"]["switch"]["tol"] = 5
    assert len(make(start={"switch": "I", "slider": 1}, steps=steps).steps) == 2


# Scoring


def test_success_through_reach_hold_reach(episode):
    result = play(episode, TASK, DIAL_OPEN + [(33.0, 35.0, "slider", 5.0)])
    assert result.outcome == "success" and result.success
    assert (result.steps_completed, result.steps_total, result.failed_step) == (3, 3, None)
    # Valve reached at 1.9 s; the wait runs 30 s from there; the slider reaches 4.85 on the 35.0 s sample.
    assert result.step_times == pytest.approx((1.9, 31.9, 35.0))
    assert result.time_to_goal_s == pytest.approx(35.0) and result.duration_s == pytest.approx(36.0)
    assert result.progress == 1.0 and result.disturbed == ()


def test_moving_on_before_the_wait_is_over_is_too_early(episode):
    result = play(episode, TASK, DIAL_OPEN + [(20.0, 22.0, "slider", 5.0)])
    assert result.outcome == "too_early" and not result.success
    assert (result.steps_completed, result.failed_step) == (1, 1)
    assert result.duration_s == pytest.approx(20.1)  # ends the moment the slider leaves its place
    assert result.disturbed == ("slider",)
    # One step done, and 18.2 s of the 30 s wait.
    assert result.progress == pytest.approx((1 + 18.2 / 30) / 3, abs=0.01)


def test_moving_on_a_little_early_is_allowed(episode):
    # The wait is 30 s with 5 s of grace: from 26.9 s on, the robot may go.
    result = play(episode, TASK, DIAL_OPEN + [(28.0, 30.0, "slider", 5.0)])
    assert result.outcome == "success"
    assert result.step_times[1] == pytest.approx(28.1)  # the wait ended when the slider left
    just_too_soon = play(episode, TASK, DIAL_OPEN + [(26.0, 28.0, "slider", 5.0)])
    assert just_too_soon.outcome == "too_early"


def test_the_wait_is_timed_from_when_the_step_before_was_reached_not_confirmed(episode):
    # Valve reached at 1.9 s, confirmed at 2.4 s. The slider leaves its place at 27.0 s:
    # 25.1 s after reaching, so just inside the allowance. Timed from confirmation it would be 24.6 s.
    result = play(episode, TASK, DIAL_OPEN + [(26.9, 28.9, "slider", 5.0)])
    assert result.outcome == "success"
    assert result.step_times[1] == pytest.approx(27.0)


def test_disturbing_something_else_during_a_wait_is_a_side_effect(episode):
    result = play(episode, TASK, DIAL_OPEN + [(10.0, 11.0, "switch", 30.0)])
    assert result.outcome == "side_effect" and result.failed_step == 1
    assert result.disturbed == ("switch",)


def test_a_reach_step_has_its_own_deadline(episode):
    idle = play(episode, TASK, [])
    assert idle.outcome == "no_contact" and (idle.steps_completed, idle.failed_step) == (0, 0)
    assert idle.duration_s == pytest.approx(20.1)  # the first sample past the 20 s allowance
    short = play(episode, TASK, [(0.0, 2.0, "dial", 30.0)])
    assert short.outcome == "partial" and short.progress == pytest.approx((20 / 37) / 3, abs=0.01)


def test_deadlines_are_relative_to_the_step_before(episode):
    # The wait ends at 31.9 s, so the slider has until 51.9 s.
    in_time = play(episode, TASK, DIAL_OPEN + [(49.0, 51.0, "slider", 5.0)])
    assert in_time.outcome == "success"
    too_late = play(episode, TASK, DIAL_OPEN + [(55.0, 57.0, "slider", 5.0)])
    assert too_late.outcome == "no_contact"
    assert (too_late.steps_completed, too_late.failed_step) == (2, 2)
    assert too_late.duration_s == pytest.approx(52.0)


def test_doing_a_later_step_first_is_wrong_order(episode):
    result = play(episode, DIAL_THEN_SLIDER, [(0.0, 2.0, "slider", 5.0), (3.0, 5.0, "dial", 50.0)])
    assert result.outcome == "wrong_order" and not result.success
    assert (result.steps_completed, result.failed_step) == (0, 0)
    assert result.final["dial"] == pytest.approx(50.0, abs=0.1)  # both ended where the task wanted them


def test_out_of_order_can_be_undone(episode):
    moves = [(0.0, 1.0, "slider", 5.0), (1.0, 2.0, "slider", 1.0), (2.0, 4.0, "dial", 50.0), (5.0, 7.0, "slider", 5.0)]
    assert play(episode, DIAL_THEN_SLIDER, moves).outcome == "success"


def test_the_robot_may_start_the_next_step_while_the_last_is_being_confirmed(episode):
    # The valve is reached at 1.9 s and the slider is already moving at 2.0 s.
    result = play(episode, DIAL_THEN_SLIDER, DIAL_OPEN + [(2.0, 4.0, "slider", 5.0)])
    assert result.outcome == "success"
    assert result.step_times == pytest.approx((1.9, 4.0))


def test_a_finished_step_must_stay_finished(episode):
    moves = DIAL_OPEN + [(3.0, 5.0, "slider", 5.0), (4.0, 5.0, "dial", 70.0)]
    result = play(episode, DIAL_THEN_SLIDER, moves)
    assert result.outcome == "side_effect"
    assert (result.steps_completed, result.failed_step, result.disturbed) == (1, 1, ("dial",))


def test_passing_through_an_intermediate_goal_does_not_complete_the_step(episode):
    task = make(start={"slider": 1, "dial": 10}, steps=[{"reach": {"slider": 3}, "within_s": 10}, {"reach": {"dial": 50}}])
    result = play(episode, task, [(0.0, 2.0, "slider", 5.0)])
    assert result.outcome == "overshoot" and result.steps_completed == 0


def test_putting_a_module_back_where_it_started(episode):
    task = make(
        start={"switch": "I", "slider": 1},
        steps=[{"reach": {"switch": "II"}}, {"reach": {"slider": 4}}, {"reach": {"switch": "start"}}],
    )
    start = {"switch": -40.0}
    there = [(0.0, 2.0, "switch", 40.0), (3.0, 5.0, "slider", 4.0)]
    done = play(episode, task, there + [(6.0, 8.0, "switch", -40.0)], start=start)
    assert done.outcome == "success" and done.steps_completed == 3
    assert done.final["switch"] == pytest.approx(-40.0, abs=0.2)
    left = play(episode, task, there, start=start, duration=100.0)
    assert left.outcome == "no_contact" and (left.steps_completed, left.failed_step) == (2, 2)
    halfway = play(episode, task, there + [(6.0, 8.0, "switch", 0.0)], start=start, duration=100.0)
    assert halfway.outcome == "partial"
    assert halfway.final_error["switch"] == pytest.approx(35.0, abs=0.2)  # 35 deg outside 'start +- 5'


def test_a_task_can_begin_with_a_wait(episode):
    task = make(start={"slider": 1}, steps=[{"hold_s": 5}, {"reach": {"slider": 3}}])
    assert play(episode, task, [(6.0, 8.0, "slider", 3.0)]).outcome == "success"
    hasty = play(episode, task, [(1.0, 3.0, "slider", 3.0)])
    assert hasty.outcome == "too_early" and hasty.steps_completed == 0


def test_a_task_that_is_only_a_wait_tests_restraint(episode):
    task = make(start={}, steps=[{"say": "Do not touch anything for 10 seconds", "hold_s": 10}])
    still = play(episode, task, [])
    assert still.outcome == "success" and still.duration_s == pytest.approx(10.0)
    fidget = play(episode, task, [(4.0, 5.0, "dial", 40.0)])
    assert fidget.outcome == "side_effect"


def test_others_ignore_frees_modules_no_step_names(episode):
    task = make(others="ignore")
    result = play(episode, task, DIAL_OPEN + [(10.0, 11.0, "switch", 30.0), (33.0, 35.0, "slider", 5.0)])
    assert result.outcome == "success"


def test_running_out_of_time_during_a_wait_is_late():
    values = {"switch": 0.0, "dial": 50.0, "plunger": 2.0, "slider": 1.0}
    task = make(start={"slider": 1}, steps=[{"hold_s": 20}, {"reach": {"slider": 3}}])
    from lebox import Sample

    result = score(task, [Sample(0.0, values), Sample(8.0, values)])  # the trace stops part way through the wait
    assert result.outcome == "late" and result.progress == pytest.approx(0.4 / 2)


def test_a_saved_episode_scores_the_same_and_the_result_is_json_ready(episode, tmp_path):
    from lebox import load_trace, save_trace

    run = episode(start={"dial": 10.0, "slider": 1.0})
    samples = run.samples(DIAL_OPEN + [(33.0, 35.0, "slider", 5.0)], duration=40.0)
    live = score(TASK, samples)
    again = score(TASK, load_trace(save_trace(tmp_path / "ep.jsonl", samples)))
    assert again == live and live.outcome in OUTCOMES
    data = json.loads(json.dumps(live.to_dict()))
    assert data["steps_total"] == 3 and data["step_times"] == pytest.approx([1.9, 31.9, 35.0])


@pytest.mark.parametrize("name", ["valve_wait_slider", "five_step_tour"])
def test_the_simulated_hand_solves_the_multi_step_builtins(name):
    import io

    from lebox.cli import main

    out = io.StringIO()
    assert main(["task", "run", "--sim", name, "--seed", "4"], out=out) == 0
    text = out.getvalue()
    total = len(load_builtin(name).steps)
    assert f"{name}: success" in text and f"steps          {total} of {total}" in text


# Tables

TABLE = """kind,say,switch,dial,plunger,slider,tol_dial,within_s,hold_s,early_s
start,,,0..30,,1|2,,,,
reach,Turn the blue valve to 50% | Set the blue valve half open,,50,,,,20,,
hold,Wait 30 seconds | Pause for half a minute,,,,,,,30,5
reach,Slide the slider to 5 | Move the white slider all the way to 5,,,,5,,20,,
"""


def test_a_table_makes_the_same_task_as_the_file():
    from_table = Task.from_dict(table_to_dict(TABLE, "valve_wait_slider"))
    builtin = load_builtin("valve_wait_slider")
    assert from_table.steps == builtin.steps and from_table.start == builtin.start
    assert from_table.signature == builtin.signature and from_table.timeout_s == builtin.timeout_s


def test_table_cells():
    table = (
        "kind,switch,dial,slider,tol_dial,within_s\n"
        "start,I,random,2\n"
        "reach,II,>=75,,,15\n"
        "reach,start,12.5,,2.5,\n"
        "reach,,<=5,4,,\n"
    )
    data = table_to_dict(table, "mixed")
    assert data["start"] == {"switch": "I", "dial": "random", "slider": 2}
    assert data["steps"] == [
        {"reach": {"switch": "II", "dial": {"min": 75}}, "within_s": 15},
        {"reach": {"switch": "start", "dial": {"target": 12.5, "tol": 2.5}}},
        {"reach": {"dial": {"max": 5}, "slider": 4}},
    ]
    assert Task.from_dict(data).signature == "switch=40±5 & dial=75..100.741 within 15s | switch=start±5 & dial=12.5±2.5 | dial=-0.740741..5 & slider=4±0.15"


@pytest.mark.parametrize(
    "table, message",
    [
        ("", "needs a header row"),
        ("say,dial\nGo,50\n", "needs a header row"),
        ("kind,dial,lever\nreach,50,1\n", "unknown column(s): lever"),
        ("kind,dial\nstart,10\n", "no reach or hold rows"),
        ("kind,dial\nstart,10\nmove,50\n", "row 3: 'kind' must be start, reach or hold"),
        ("kind,dial,say\nstart,10,\nreach,,Go\n", "row 3: a reach step needs a position"),
        ("kind,dial,hold_s\nstart,10,\nhold,,\n", "row 3: a hold step needs 'hold_s'"),
        ("kind,dial\nreach,50\n", "every goal module needs a start"),
        ("kind,dial\nstart,10\nreach,150\n", "outside the working range"),
    ],
)
def test_mistakes_in_a_table_are_reported(table, message):
    with pytest.raises(TaskError) as error:
        table_to_dict(table, "t")
    assert message in str(error.value)


def test_movement_made_while_the_last_step_was_being_confirmed_still_counts(episode):
    # The slider is moved part of the way within half a second of the valve arriving, then left there.
    result = play(episode, DIAL_THEN_SLIDER, DIAL_OPEN + [(1.9, 2.3, "slider", 3.0)], duration=70.0)
    assert result.outcome == "partial"  # not no_contact: the slider did move during this step
    assert (result.steps_completed, result.failed_step) == (1, 1)
