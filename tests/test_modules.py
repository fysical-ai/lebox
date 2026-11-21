import math

import pytest

from lebox.modules import DEFAULT_MODULES, DIAL, PLUNGER, SLIDER, SWITCH, ModuleError, ModuleSpec, Marking


def test_default_board_is_the_four_v02_modules_on_ids_1_to_4():
    assert [(m.name, m.servo_id) for m in DEFAULT_MODULES] == [
        ("switch", 1),
        ("dial", 2),
        ("plunger", 3),
        ("slider", 4),
    ]


def test_design_travel_matches_the_design_spec():
    # Switch stops at +-42 deg, dial stops at +-137 deg (direct drive).
    assert SWITCH.servo_travel_deg == 84.0
    assert DIAL.servo_travel_deg == 274.0
    # Plunger: 20 mm on a 34.5 mm sector; slider: 40.6 mm on a 36 mm sector.
    assert PLUNGER.servo_travel_deg == pytest.approx(33.21, abs=0.01)
    assert SLIDER.servo_travel_deg == pytest.approx(64.62, abs=0.01)
    # All under one revolution, so position is absolute with no homing.
    assert all(m.servo_travel_deg < 360 for m in DEFAULT_MODULES)


def test_expected_travel_in_encoder_counts():
    assert SWITCH.expected_travel_ticks == pytest.approx(84 / 360 * 4096)
    assert DIAL.expected_travel_ticks == pytest.approx(3117.5, abs=0.1)


@pytest.mark.parametrize("spec", DEFAULT_MODULES, ids=lambda m: m.name)
def test_raw_and_value_convert_both_ways(spec):
    raw_lo, raw_hi = 1000, 2500
    assert spec.value_from_raw(raw_lo, raw_lo, raw_hi) == pytest.approx(spec.stops[0])
    assert spec.value_from_raw(raw_hi, raw_lo, raw_hi) == pytest.approx(spec.stops[1])
    for value in (spec.work[0], sum(spec.work) / 2, spec.work[1]):
        raw = spec.raw_from_value(value, raw_lo, raw_hi)
        assert spec.value_from_raw(raw, raw_lo, raw_hi) == pytest.approx(value)


def test_a_servo_mounted_the_other_way_round_still_reads_correctly():
    # raw_lo above raw_hi: the encoder counts down as the scale goes up.
    assert SLIDER.value_from_raw(3000, 3000, 2000) == pytest.approx(SLIDER.stops[0])
    assert SLIDER.value_from_raw(2500, 3000, 2000) == pytest.approx(3.0)


def test_dial_scale_is_percent_of_the_driven_270_degrees():
    raw_lo, raw_hi = 0.0, 274.0  # one count per degree, stop to stop
    assert DIAL.value_from_raw(2.0, raw_lo, raw_hi) == pytest.approx(0.0)  # CLOSED, 2 deg off the stop
    assert DIAL.value_from_raw(137.0, raw_lo, raw_hi) == pytest.approx(50.0)
    assert DIAL.value_from_raw(272.0, raw_lo, raw_hi) == pytest.approx(100.0)  # OPEN


def test_slider_marks_are_10_mm_apart_with_stops_just_outside():
    raw_lo, raw_hi = 0.0, 40.6  # one count per mm, slot end to slot end
    assert SLIDER.value_from_raw(0.3, raw_lo, raw_hi) == pytest.approx(1.0)
    assert SLIDER.value_from_raw(20.3, raw_lo, raw_hi) == pytest.approx(3.0)
    assert SLIDER.value_from_raw(40.3, raw_lo, raw_hi) == pytest.approx(5.0)


def test_switch_labels():
    assert SWITCH.label_at(-40.0) == "I"
    assert SWITCH.label_at(2.0) == "O"
    assert SWITCH.label_at(38.0) == "II"
    assert SWITCH.label_at(20.0) is None  # between O and II


def test_dial_labels():
    assert DIAL.label_at(0.5) == "CLOSED"
    assert DIAL.label_at(99.0) == "OPEN"
    assert DIAL.label_at(51.0) == "50"
    assert DIAL.label_at(37.0) is None


@pytest.mark.parametrize(
    "spec, target, expected",
    [
        (SWITCH, "II", 40.0),
        (SWITCH, "o", 0.0),
        (DIAL, "closed", 0.0),
        (DIAL, "OPEN", 100.0),
        (DIAL, "25", 25.0),
        (DIAL, 62.5, 62.5),
        (SLIDER, "2.5", 2.5),
        (SLIDER, 5, 5.0),
        (PLUNGER, " 2 ", 2.0),
    ],
)
def test_resolve_accepts_labels_and_numbers(spec, target, expected):
    assert spec.resolve(target) == pytest.approx(expected)


@pytest.mark.parametrize(
    "spec, target",
    [
        (SLIDER, 5.5),
        (SLIDER, 0.9),
        (SWITCH, 41),  # between the working range and the stop
        (DIAL, -1),
        (DIAL, "half"),
        (PLUNGER, float("nan")),
        (PLUNGER, "inf"),
    ],
)
def test_resolve_rejects_targets_outside_the_working_range(spec, target):
    with pytest.raises(ModuleError):
        spec.resolve(target)


def test_plunger_end_rings_sit_on_the_stops_so_targets_stay_just_inside():
    # Rings 0 and 4 are at the hard stops; the software must not drive into them.
    assert PLUNGER.stops == (0.0, 4.0)
    assert PLUNGER.resolve("0") == pytest.approx(0.1)
    assert PLUNGER.resolve(0) == pytest.approx(0.1)
    assert PLUNGER.resolve(4.0) == pytest.approx(3.9)
    assert PLUNGER.label_at(0.1) == "0"
    assert PLUNGER.label_at(3.9) == "4"
    # A number that is not a marking gets no such allowance.
    with pytest.raises(ModuleError):
        PLUNGER.resolve(0.05)


def test_zero_travel_calibration_is_an_error_not_a_division_by_zero():
    with pytest.raises(ModuleError):
        SLIDER.value_from_raw(2000, 2000, 2000)


def test_spec_rejects_a_work_range_outside_the_stops():
    with pytest.raises(ValueError):
        ModuleSpec(
            name="bad",
            servo_id=9,
            unit="",
            stops=(0.0, 1.0),
            work=(-0.5, 1.0),
            markings=(Marking("0", 0.0),),
            servo_travel_deg=10.0,
            snap=0.1,
        )


def test_gear_arc_helper_matches_hand_calculation():
    assert PLUNGER.servo_travel_deg == pytest.approx(math.degrees(20.0 / 34.5))
