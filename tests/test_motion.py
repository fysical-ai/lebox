import pytest

from lebox import (
    Board,
    Calibration,
    CalibrationError,
    ModuleError,
    MotionError,
    MotionSettings,
    SimBus,
    SimClock,
    StallError,
    drive,
)
from lebox.modules import DEFAULT_MODULES, DIAL, SLIDER, SWITCH

CAPPED = 300


@pytest.fixture
def rig():
    """A simulated board as `lebox setup-motors` leaves it: torque caps stored."""
    calibration = Calibration.from_design()
    bus = SimBus.for_board(calibration, start={"slider": 1.0})
    for spec in DEFAULT_MODULES:
        bus.write_torque_cap(spec.servo_id, CAPPED)
    bus.power_cycle()
    return Board(bus, calibration), bus, SimClock(bus)


def test_drive_reaches_the_target_and_releases(rig):
    board, bus, clock = rig
    target = board.raw_for("slider", 4)
    final = drive(bus, SLIDER.servo_id, target, clock=clock)
    assert abs(final - target) <= MotionSettings().tolerance_ticks
    assert not bus.torque_enabled(SLIDER.servo_id)
    assert bus.read_torque_limit(SLIDER.servo_id) == 150  # the working limit, below the cap


def test_drive_works_in_both_directions(rig):
    board, bus, clock = rig
    for value in (5, 1, 3):
        target = board.raw_for("slider", value)
        assert abs(drive(bus, SLIDER.servo_id, target, clock=clock) - target) <= 6


def test_drive_never_commands_far_ahead_of_the_present_position(rig):
    board, bus, clock = rig
    leads = []
    original = bus.write_goal

    def spy(servo_id, raw):
        leads.append(abs(raw - bus.read_position(servo_id)))
        original(servo_id, raw)

    bus.write_goal = spy
    drive(bus, SLIDER.servo_id, board.raw_for("slider", 5), clock=clock)
    assert max(leads) <= MotionSettings().lead_ticks
    assert leads[0] == 0  # torque is enabled with the goal on the present position


def test_drive_refuses_a_servo_without_a_stored_cap():
    calibration = Calibration.from_design()
    bus = SimBus.for_board(calibration)  # factory state: cap at 100 %
    with pytest.raises(MotionError, match="stored torque cap of 100 %"):
        drive(bus, SLIDER.servo_id, 2100, clock=SimClock(bus))
    assert not bus.torque_enabled(SLIDER.servo_id)
    assert bus.read_torque_limit(SLIDER.servo_id) == 1000  # nothing was written


def test_drive_refuses_if_the_torque_limit_does_not_read_back(rig):
    board, bus, clock = rig
    bus.write_torque_limit = lambda servo_id, permille: None  # the write is lost
    with pytest.raises(MotionError, match="did not take the torque limit"):
        drive(bus, SLIDER.servo_id, board.raw_for("slider", 4), clock=clock)
    assert not bus.torque_enabled(SLIDER.servo_id)


def test_the_working_limit_never_exceeds_the_stored_cap(rig):
    board, bus, clock = rig
    bus.write_torque_cap(SLIDER.servo_id, 100)
    drive(bus, SLIDER.servo_id, board.raw_for("slider", 4), MotionSettings(torque=350), clock)
    assert bus.read_torque_limit(SLIDER.servo_id) == 100


def test_a_blocked_module_stalls_and_is_released_quickly(rig):
    board, bus, clock = rig
    bus.hold(SLIDER.servo_id)
    before = bus.time
    with pytest.raises(StallError) as error:
        drive(bus, SLIDER.servo_id, board.raw_for("slider", 4), clock=clock)
    assert not bus.torque_enabled(SLIDER.servo_id)
    assert bus.time - before < 0.5  # stall_s is 0.25
    assert error.value.servo_id == SLIDER.servo_id and "torque released" in str(error.value)


def test_a_module_blocked_part_way_stalls_where_it_stopped(rig):
    board, bus, clock = rig
    target = board.raw_for("slider", 5)
    halfway = board.raw_for("slider", 3)
    watching = SimClock(bus, on_tick=lambda t: bus.hold(SLIDER.servo_id, bus.read_position(SLIDER.servo_id) >= halfway))
    with pytest.raises(StallError) as error:
        drive(bus, SLIDER.servo_id, target, clock=watching)
    assert halfway <= error.value.position < target
    assert not bus.torque_enabled(SLIDER.servo_id)


def test_too_little_torque_reads_as_a_stall(rig):
    board, bus, clock = rig
    with pytest.raises(StallError, match="torque limit is too low"):
        drive(bus, SLIDER.servo_id, board.raw_for("slider", 4), MotionSettings(torque=30), clock)


def test_torque_is_released_even_if_the_bus_fails_mid_move(rig):
    board, bus, clock = rig
    calls = {"n": 0}
    original = bus.read_position

    def flaky(servo_id):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("cable pulled")
        return original(servo_id)

    bus.read_position = flaky
    with pytest.raises(RuntimeError):
        drive(bus, SLIDER.servo_id, board.raw_for("slider", 5), clock=clock)
    assert not bus.torque_enabled(SLIDER.servo_id)


def test_drive_times_out_on_a_servo_that_only_crawls(rig):
    board, bus, clock = rig
    bus._servo(SLIDER.servo_id).speed = 20.0  # moving, so not a stall, but far too slow
    with pytest.raises(MotionError, match="did not reach"):
        drive(bus, SLIDER.servo_id, board.raw_for("slider", 5), MotionSettings(timeout_s=2.0), clock)
    assert not bus.torque_enabled(SLIDER.servo_id)


@pytest.mark.parametrize("kwargs", [{"torque": 0}, {"torque": 500}, {"max_cap": 1200}, {"lead_ticks": 4}])
def test_unsafe_settings_are_rejected(kwargs):
    with pytest.raises(MotionError):
        MotionSettings(**kwargs)


def test_board_move_takes_values_and_markings_and_reports_where_things_ended(rig):
    board, bus, clock = rig
    results = board.move({"slider": 4, "switch": "II", "dial": "closed"}, clock=clock)
    assert list(results) == ["slider", "switch", "dial"]
    assert results["slider"].reached == pytest.approx(4.0, abs=0.05)
    assert results["switch"].reached == pytest.approx(40.0, abs=0.6)
    assert results["dial"].reached == pytest.approx(0.0, abs=0.3)
    assert all(abs(result.error_ticks) <= 6 and result.seconds > 0 for result in results.values())
    assert all(not bus.torque_enabled(spec.servo_id) for spec in DEFAULT_MODULES)
    readings = board.read()
    assert (readings["slider"].label, readings["switch"].label, readings["dial"].label) == ("4", "II", "CLOSED")


def test_board_move_checks_every_target_before_anything_moves(rig):
    board, bus, clock = rig
    before = board.values()
    with pytest.raises(ModuleError):
        board.move({"slider": 4, "dial": 250}, clock=clock)
    assert board.values() == before


def test_board_move_leaves_everything_passive_after_a_stall(rig):
    board, bus, clock = rig
    bus.hold(SWITCH.servo_id)
    with pytest.raises(StallError):
        board.move({"slider": 4, "switch": "II", "dial": "open"}, clock=clock)
    assert all(not bus.torque_enabled(spec.servo_id) for spec in DEFAULT_MODULES)
    assert board.read_module("slider").label == "4"  # done before the stall
    assert board.read_module("dial").label == "50"  # never started


def test_reset_puts_a_task_start_in_place(rig):
    import random

    from lebox import load_builtin

    board, bus, clock = rig
    task = load_builtin("dial_open")
    start = task.sample_start(random.Random(4))
    results = board.reset(start, clock=clock)
    assert results["dial"].reached == pytest.approx(start["dial"], abs=0.3)
    assert not task.goals[0].satisfied(board.read_module("dial").value)


def test_reset_reports_a_module_that_does_not_stay_put(rig):
    board, bus, clock = rig
    original = bus.set_torque

    def springy(servo_id, enabled):
        original(servo_id, enabled)
        if not enabled and servo_id == DIAL.servo_id:
            bus.push(servo_id, -400)  # springs back when released

    bus.set_torque = springy
    with pytest.raises(MotionError, match="did not hold"):
        board.reset({"dial": 75}, clock=clock)


def test_bench_calibration_centres_the_travel_on_the_present_position():
    positions = {"switch": 2048, "dial": 2000, "plunger": 700, "slider": 3300}
    calibration = Calibration.from_bench(positions)
    assert calibration.bench and not calibration.nominal and calibration.board_id == "bench"
    for spec in DEFAULT_MODULES:
        entry = calibration.modules[spec.name]
        assert (entry.raw_lo + entry.raw_hi) / 2 == pytest.approx(positions[spec.name], abs=1)
        assert entry.travel_ticks == pytest.approx(spec.expected_travel_ticks, abs=1)
        assert 0 <= entry.raw_lo < entry.raw_hi <= 4095
    assert calibration.problems() == []


def test_bench_calibration_survives_a_save_and_load(tmp_path):
    calibration = Calibration.from_bench({"switch": 2048, "dial": 2048, "plunger": 2048, "slider": 2048})
    loaded = Calibration.load(calibration.save(tmp_path / "bench.json"))
    assert loaded.bench and loaded.modules == calibration.modules


def test_bench_calibration_needs_room_for_the_whole_travel():
    with pytest.raises(CalibrationError) as error:
        Calibration.from_bench({"switch": 2048, "dial": 900, "plunger": 2048, "slider": 2048})
    message = str(error.value)
    assert message.startswith("dial: the horn is at 900") and "between 1599 and 2496" in message
    with pytest.raises(CalibrationError, match="slider: no position given"):
        Calibration.from_bench({"switch": 2048, "dial": 2048, "plunger": 2048})


def test_power_cycle_reloads_the_working_limit_from_the_cap(rig):
    board, bus, clock = rig
    bus.write_torque_limit(SLIDER.servo_id, 1000)
    bus.set_torque(SLIDER.servo_id, True)
    bus.power_cycle()
    assert bus.read_torque_limit(SLIDER.servo_id) == CAPPED
    assert not bus.torque_enabled(SLIDER.servo_id)
