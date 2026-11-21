import dataclasses

import pytest

from lebox import Board, BusError, Calibration, CalibrationError, ModuleCalibration, ModuleError, SimBus
from lebox.bus import demo_positions
from lebox.modules import DEFAULT_MODULES, SLIDER, SWITCH


@pytest.fixture
def calibration():
    return Calibration.from_design()


@pytest.fixture
def bus(calibration):
    return SimBus.for_board(calibration)


@pytest.fixture
def board(bus, calibration):
    return Board(bus, calibration)


def test_board_starts_mid_range_and_reads_in_real_units(board):
    readings = board.read()
    assert list(readings) == ["switch", "dial", "plunger", "slider"]
    assert readings["switch"].value == pytest.approx(0.0, abs=0.1)
    assert readings["switch"].label == "O"
    assert readings["dial"].value == pytest.approx(50.0, abs=0.1)
    assert readings["dial"].label == "50"
    assert readings["plunger"].value == pytest.approx(2.0, abs=0.02)
    assert readings["slider"].value == pytest.approx(3.0, abs=0.02)
    assert all(r.in_work_range for r in readings.values())


def test_start_state_can_be_given_per_module(calibration):
    bus = SimBus.for_board(calibration, start={"slider": 1.0, "switch": 40.0})
    values = Board(bus, calibration).values()
    assert values["slider"] == pytest.approx(1.0, abs=0.01)
    assert values["switch"] == pytest.approx(40.0, abs=0.1)
    assert values["dial"] == pytest.approx(50.0, abs=0.1)


def test_unknown_module_in_start_is_rejected(calibration):
    with pytest.raises(KeyError, match="lever"):
        SimBus.for_board(calibration, start={"lever": 1.0})


def test_pushing_a_module_changes_its_reading(board, bus):
    cal = board.calibration.modules["slider"]
    ticks_per_mark = (cal.raw_hi - cal.raw_lo) / (SLIDER.stops[1] - SLIDER.stops[0])
    bus.push(SLIDER.servo_id, ticks_per_mark)  # one mark up
    reading = board.read_module("slider")
    assert reading.value == pytest.approx(4.0, abs=0.01)
    assert reading.label == "4"


def test_pushing_past_a_hard_stop_stops_at_the_stop(board, bus):
    moved = bus.push(SWITCH.servo_id, 10_000)
    cal = board.calibration.modules["switch"]
    assert moved == pytest.approx(cal.raw_hi - 2048, abs=1)
    reading = board.read_module("switch")
    assert reading.value == pytest.approx(42.0, abs=0.1)  # at the stop, 2 deg past II
    assert reading.label == "II"
    assert reading.in_work_range is False


def test_a_module_held_with_torque_does_not_move_when_pushed(board, bus):
    bus.set_torque(SLIDER.servo_id, True)
    assert bus.push(SLIDER.servo_id, 100) == 0.0
    board.passive()
    assert all(not bus.torque_enabled(spec.servo_id) for spec in DEFAULT_MODULES)
    assert bus.push(SLIDER.servo_id, 100) == pytest.approx(100)


def test_raw_for_converts_targets_to_encoder_positions(board):
    cal = board.calibration.modules["slider"]
    assert board.raw_for("slider", 3) == pytest.approx((cal.raw_lo + cal.raw_hi) / 2, abs=1)
    assert board.raw_for("switch", "O") == pytest.approx(2048, abs=1)
    assert board.raw_for("dial", "closed") < board.raw_for("dial", "open")
    with pytest.raises(ModuleError):
        board.raw_for("slider", 6)
    with pytest.raises(KeyError, match="lever"):
        board.raw_for("lever", 1)


def test_a_reversed_servo_reads_the_same_scale(calibration):
    normal = calibration.modules["slider"]
    calibration.modules["slider"] = ModuleCalibration(normal.raw_hi, normal.raw_lo)
    bus = SimBus.for_board(calibration, start={"slider": 2.0})
    board = Board(bus, calibration)
    assert board.read_module("slider").value == pytest.approx(2.0, abs=0.01)
    bus.push(SLIDER.servo_id, -10_000)  # encoder counts down, scale goes up
    assert board.read_module("slider").value == pytest.approx(SLIDER.stops[1], abs=0.01)


def test_board_refuses_a_calibration_that_misses_a_module(bus):
    partial = Calibration(board_id="half", modules={"switch": ModuleCalibration(1570, 2526)})
    with pytest.raises(CalibrationError, match="dial, plunger, slider"):
        Board(bus, partial)


def test_board_rejects_duplicate_servo_ids(bus, calibration):
    clash = dataclasses.replace(SLIDER, servo_id=SWITCH.servo_id)
    with pytest.raises(ValueError, match="servo ids"):
        Board(bus, calibration, specs=(SWITCH, clash))
    with pytest.raises(ValueError, match="module names"):
        Board(bus, calibration, specs=(SWITCH, SWITCH))


def test_a_servo_that_does_not_answer_raises_bus_error(calibration):
    bus = SimBus.for_board(calibration, specs=DEFAULT_MODULES[:3])  # slider servo unplugged
    board = Board(bus, calibration)
    with pytest.raises(BusError, match="id 4"):
        board.read()


def test_closing_the_board_closes_the_bus(bus, calibration):
    with Board(bus, calibration) as board:
        board.read()
    assert bus.closed
    with pytest.raises(BusError, match="closed"):
        bus.read_position(1)


def test_encoder_noise_stays_within_its_bound(calibration):
    bus = SimBus.for_board(calibration, noise_ticks=2.0, seed=1)
    readings = {bus.read_position(SLIDER.servo_id) for _ in range(200)}
    assert max(readings) - min(readings) <= 4
    assert len(readings) > 1


def test_demo_motion_starts_mid_range_and_stays_in_the_working_range(calibration):
    bus = SimBus.for_board(calibration)
    board = Board(bus, calibration)
    for step in range(0, 400):
        for servo_id, raw in demo_positions(calibration, step * 0.1).items():
            bus.place(servo_id, raw)
        readings = board.read()
        if step == 0:
            assert readings["dial"].value == pytest.approx(50.0, abs=0.1)
        for spec in DEFAULT_MODULES:
            value = readings[spec.name].value
            slack = (spec.stops[1] - spec.stops[0]) / spec.expected_travel_ticks  # one count
            assert spec.work[0] - slack <= value <= spec.work[1] + slack
