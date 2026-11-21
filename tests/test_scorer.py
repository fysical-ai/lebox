import json

import pytest

from lebox import OUTCOMES, Sample, ScoreError, Scorer, Task, load_builtin, load_trace, save_trace, score

SLIDER_TO_3 = load_builtin("slider_to_3")


def task(**changes):
    data = {
        "name": "t",
        "instruction": "Move the slider to 3",
        "start": {"slider": 1},
        "goal": {"slider": 3},
        **changes,
    }
    return Task.from_dict(data)


def test_success_when_the_goal_is_reached_and_held(episode):
    result = episode().play(SLIDER_TO_3, [(0.0, 2.0, "slider", 3.0)])
    assert result.outcome == "success" and result.success
    # The slider crosses 2.85 at 1.85 s; the first 10 Hz sample in the goal is at 1.9 s.
    assert result.time_to_goal_s == pytest.approx(1.9)
    assert result.duration_s == pytest.approx(2.9)  # one second of hold, then the episode ends
    assert result.progress == 1.0 and result.best_progress == 1.0
    assert result.final_error == {"slider": 0.0}
    assert result.moved == ("slider",) and result.disturbed == ()
    assert result.start["slider"] == pytest.approx(1.0, abs=0.01)
    assert result.final["slider"] == pytest.approx(3.0, abs=0.01)


def test_no_contact_when_nothing_moves(episode):
    result = episode().play(SLIDER_TO_3)
    assert result.outcome == "no_contact" and not result.success
    assert result.duration_s == pytest.approx(30.0)  # ran to the timeout
    assert result.progress == 0.0 and result.moved == ()
    assert result.time_to_goal_s is None


def test_encoder_noise_alone_is_not_contact(episode):
    result = episode(noise_ticks=2.0).play(SLIDER_TO_3)
    assert result.outcome == "no_contact" and result.moved == ()


def test_partial_when_the_module_moves_but_stops_short(episode):
    result = episode().play(SLIDER_TO_3, [(0.0, 1.0, "slider", 2.0)])
    assert result.outcome == "partial"
    # Started 1.85 below the goal's edge (2.85), ended 0.85 below it.
    assert result.progress == pytest.approx(1 - 0.85 / 1.85, abs=0.01)
    assert result.final_error["slider"] == pytest.approx(0.85, abs=0.01)


def test_overshoot_when_the_goal_is_passed_without_holding(episode):
    result = episode().play(SLIDER_TO_3, [(0.0, 2.0, "slider", 5.0)])
    assert result.outcome == "overshoot"
    assert result.best_progress == 1.0  # it was in the goal on the way past
    assert result.progress == 0.0  # and ended as far away as it started
    assert result.final_error["slider"] == pytest.approx(5.0 - 3.15, abs=0.01)


def test_wrong_module_when_only_another_module_moves(episode):
    result = episode().play(SLIDER_TO_3, [(0.0, 1.0, "dial", 80.0)])
    assert result.outcome == "wrong_module"
    assert result.moved == ("dial",) and result.disturbed == ("dial",)


def test_side_effect_when_the_goal_is_reached_but_another_module_is_left_displaced(episode):
    moves = [(0.0, 1.0, "dial", 80.0), (1.0, 3.0, "slider", 3.0)]
    result = episode().play(SLIDER_TO_3, moves)
    assert result.outcome == "side_effect" and not result.success
    assert result.duration_s == pytest.approx(30.0)  # never counted as done
    assert result.disturbed == ("dial",) and result.progress == 1.0


def test_a_disturbance_that_is_put_back_does_not_spoil_success(episode):
    moves = [(0.0, 1.0, "dial", 80.0), (1.0, 3.0, "slider", 3.0), (4.0, 5.0, "dial", 50.0)]
    result = episode().play(SLIDER_TO_3, moves)
    assert result.outcome == "success"
    assert result.moved == ("dial", "slider") and result.disturbed == ()
    # The hold only starts once the dial is back within tolerance (47 %), at 4.9 s.
    assert result.time_to_goal_s == pytest.approx(4.9)


def test_others_ignore_allows_touching_other_modules(episode):
    moves = [(0.0, 1.0, "dial", 80.0), (1.0, 3.0, "slider", 3.0)]
    result = episode().play(task(others="ignore"), moves)
    assert result.outcome == "success" and result.disturbed == ()


def test_others_tol_sets_how_much_another_module_may_move(episode):
    moves = [(0.0, 1.0, "dial", 58.0), (1.0, 3.0, "slider", 3.0)]
    assert episode().play(task(), moves).outcome == "side_effect"  # default dial tolerance is 3 %
    assert episode().play(task(others_tol={"dial": 10}), moves).outcome == "success"


def test_late_when_the_goal_is_reached_just_before_the_timeout(episode):
    result = episode().play(SLIDER_TO_3, [(27.5, 29.5, "slider", 3.0)])
    assert result.outcome == "late" and not result.success
    assert result.progress == 1.0 and result.duration_s == pytest.approx(30.0)


def test_success_arriving_after_the_timeout_does_not_count(episode):
    samples = episode().samples([(29.5, 30.5, "slider", 3.0)], duration=40.0)
    result = score(SLIDER_TO_3, samples)
    assert result.outcome == "partial"
    assert result.duration_s == pytest.approx(30.0)


def test_the_hold_restarts_if_the_goal_is_left(episode):
    moves = [(0.0, 1.0, "slider", 3.0), (1.5, 2.0, "slider", 4.0), (3.0, 4.0, "slider", 3.0)]
    result = episode().play(SLIDER_TO_3, moves)
    assert result.outcome == "success"
    assert result.time_to_goal_s == pytest.approx(3.9)  # the second entry, not the first


def test_hold_zero_succeeds_on_the_first_sample_in_the_goal(episode):
    result = episode().play(task(hold_s=0), [(0.0, 2.0, "slider", 5.0)])
    assert result.outcome == "success"
    # Moving 1 -> 5 in 2 s, the slider is at 3.0 exactly on the 1.0 s sample.
    assert result.time_to_goal_s == pytest.approx(1.0) and result.duration_s == pytest.approx(1.0)


def test_all_goal_modules_must_be_in_place_together(episode):
    both = task(start={"slider": 1, "switch": "O"}, goal={"slider": 3, "switch": "II"})
    only_slider = episode().play(both, [(0.0, 1.0, "slider", 3.0)])
    assert only_slider.outcome == "partial"
    assert only_slider.progress == pytest.approx(0.5)  # one of two goals met
    assert only_slider.final_error["slider"] == 0.0 and only_slider.final_error["switch"] > 30
    done = episode().play(both, [(0.0, 1.0, "slider", 3.0), (1.0, 2.0, "switch", 40.0)])
    assert done.outcome == "success"


def test_one_sided_goal(episode):
    open_enough = task(start={"dial": 10}, goal={"dial": {"min": 75}})
    result = episode(start={"dial": 10.0}).play(open_enough, [(0.0, 2.0, "dial", 90.0)])
    assert result.outcome == "success"
    short = episode(start={"dial": 10.0}).play(open_enough, [(0.0, 2.0, "dial", 60.0)])
    assert short.outcome == "partial"
    assert short.progress == pytest.approx(50 / 65, abs=0.01)


def test_plunger_pushed_onto_its_stop_counts_as_pushed_in(episode):
    push = load_builtin("plunger_push")
    result = episode(start={"plunger": 3.0}).play(push, [(0.0, 1.0, "plunger", 0.0)])
    assert result.outcome == "success"


def test_abort_marks_the_episode_and_overrides_the_outcome(episode):
    samples = episode().samples([(0.0, 2.0, "slider", 2.0)], duration=1.0)
    result = score(SLIDER_TO_3, samples, abort_reason="reset stalled")
    assert result.outcome == "aborted" and not result.success
    assert result.abort_reason == "reset stalled"
    assert result.duration_s == pytest.approx(1.0)


def test_an_episode_that_already_succeeded_is_not_turned_into_an_abort(episode):
    samples = episode().samples([(0.0, 2.0, "slider", 3.0)], duration=5.0)
    assert score(SLIDER_TO_3, samples, abort_reason="operator stop").outcome == "success"


def test_the_live_scorer_stops_at_success_and_ignores_later_samples(episode):
    scorer = Scorer(SLIDER_TO_3)
    done_at = None
    for sample in episode().samples([(0.0, 2.0, "slider", 3.0), (5.0, 6.0, "slider", 5.0)], duration=8.0):
        if scorer.update(sample.t, sample.values) and done_at is None:
            done_at = sample.t
    assert done_at == pytest.approx(2.9) and scorer.done
    assert scorer.result().outcome == "success"  # the later move away changed nothing


def test_scoring_a_saved_trace_gives_the_same_result(episode, tmp_path):
    run = episode()
    samples = run.samples([(0.0, 2.0, "slider", 3.0)], duration=5.0)
    live = score(SLIDER_TO_3, samples)
    path = save_trace(tmp_path / "runs" / "ep0.jsonl", samples)
    assert score(SLIDER_TO_3, load_trace(path)) == live


def test_a_trace_can_be_scored_again_under_a_different_definition(episode):
    samples = episode().samples([(0.0, 2.0, "slider", 2.7)], duration=35.0)
    assert score(task(), samples).outcome == "partial"  # default tolerance 0.15
    loose = task(goal={"slider": {"target": 3, "tol": 0.4}})
    assert score(loose, samples).outcome == "success"


def test_times_are_measured_from_the_first_sample():
    values = {"switch": 0.0, "dial": 50.0, "plunger": 2.0, "slider": 1.0}
    reached = {**values, "slider": 3.0}
    samples = [Sample(100.0, values), Sample(101.0, reached), Sample(102.0, reached)]
    result = score(SLIDER_TO_3, samples)
    assert result.outcome == "success"
    assert result.time_to_goal_s == pytest.approx(1.0) and result.duration_s == pytest.approx(2.0)


def test_a_trace_that_ends_early_is_scored_as_far_as_it_goes():
    values = {"switch": 0.0, "dial": 50.0, "plunger": 2.0, "slider": 1.0}
    result = score(SLIDER_TO_3, [Sample(0.0, values), Sample(0.5, {**values, "slider": 3.0})])
    assert result.outcome == "late" and result.duration_s == pytest.approx(0.5)


def test_errors():
    values = {"switch": 0.0, "dial": 50.0, "plunger": 2.0, "slider": 1.0}
    scorer = Scorer(SLIDER_TO_3)
    with pytest.raises(ScoreError, match="no samples"):
        scorer.result()
    with pytest.raises(ScoreError, match="no value for: dial"):
        scorer.update(0.0, {name: value for name, value in values.items() if name != "dial"})
    scorer.update(1.0, values)
    with pytest.raises(ScoreError, match="time order"):
        scorer.update(0.5, values)


def test_result_is_json_ready_and_uses_a_known_outcome(episode):
    result = episode().play(SLIDER_TO_3, [(0.0, 2.0, "slider", 3.0)])
    data = json.loads(json.dumps(result.to_dict()))
    assert data["outcome"] in OUTCOMES and data["moved"] == ["slider"]
    assert set(data) >= {"task", "success", "duration_s", "progress", "final_error", "start", "final"}
