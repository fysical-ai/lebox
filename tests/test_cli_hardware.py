"""The commands that touch a board, run on the simulator and on an emulated serial bus."""

import io
import json

import pytest

from fake_serial import FakeSerial, new_memory
from lebox import Calibration, load_trace
from lebox.cli import Hooks, main
from lebox.feetech import FeetechBus


class InstantClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def run(*argv, hooks=None):
    out = io.StringIO()
    code = main(list(argv), out=out, hooks=hooks)
    return code, out.getvalue()


def serial_hooks(fake, ask=None):
    def open_bus(port):
        return FeetechBus(port, port_factory=lambda *_: fake, sleep=lambda dt: None)

    return Hooks(ask=ask or (lambda prompt: ""), open_bus=open_bus, clock=InstantClock())


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEBOX_HOME", str(tmp_path))
    return tmp_path


def capped_board():
    fake = FakeSerial((1, 2, 3, 4))
    fake.motors = True
    for servo_id in fake.servos:
        fake.set(servo_id, 16, 300, size=2)
    fake.power_cycle()
    return fake


def test_a_command_without_a_board_explains_the_two_choices(capsys):
    code, text = run("ping")
    assert code == 2 and text == ""
    error = capsys.readouterr().err
    assert "--port" in error and "--sim" in error


def test_ping_on_the_simulated_board_lists_four_capped_servos():
    code, text = run("ping", "--sim")
    assert code == 0
    lines = text.splitlines()
    assert lines[0].split() == ["id", "model", "position", "volts", "temp", "torque", "limit", "cap"]
    assert [line.split()[0] for line in lines[1:]] == ["1", "2", "3", "4"]
    assert "30 %   30 %" in lines[1] and "off" in lines[1]


def test_ping_on_a_real_bus_reads_only_and_warns_about_a_missing_cap(home):
    fake = FakeSerial((1,))
    code, text = run("ping", "--port", "/dev/fake", hooks=serial_hooks(fake))
    assert code == 0
    assert "  1    777      2048    5.0  25 C  off      100 %  100 %" in text
    assert "No torque cap stored on id 1" in text
    # Nothing was written to any servo: every packet sent was a ping or a read.
    assert {packet[4] for packet in fake.written} <= {0x01, 0x02}
    assert not fake.is_open


def test_ping_reports_when_nothing_answers(home):
    code, text = run("ping", "--port", "/dev/fake", hooks=serial_hooks(FakeSerial(())))
    assert code == 1 and "No servo answered" in text


def test_setup_motors_gives_each_servo_its_id_and_cap_one_at_a_time(home):
    fake = FakeSerial(())
    shelf = []
    prompts = []

    def plug_in_the_next_servo(prompt):
        prompts.append(prompt)
        shelf.extend(fake.servos.values())  # unplug the one just done
        fake.servos.clear()
        fake.servos[1] = new_memory(1)  # a new servo always arrives as id 1
        return ""

    code, text = run("setup-motors", "--port", "/dev/fake", hooks=serial_hooks(fake, plug_in_the_next_servo))
    assert code == 0
    assert [name in prompt for name, prompt in zip(["switch", "dial", "plunger", "slider"], prompts)] == [True] * 4
    done = shelf + list(fake.servos.values())
    assert [memory[5] for memory in done] == [1, 2, 3, 4]
    assert all(int.from_bytes(memory[16:18], "little") == 300 for memory in done)
    assert all(memory[55] == 1 for memory in done)  # every servo locked again
    assert fake.lock_violations == 0
    assert "dial: id 1 -> 2, torque cap 30 %" in text and "switch: id 1, torque cap 30 %" in text
    assert "Switch the servo power off and on" in text


def test_setup_motors_insists_on_exactly_one_servo(home):
    fake = FakeSerial((1, 2))
    answers = iter(["", "q"])
    code, text = run("setup-motors", "--port", "/dev/fake", hooks=serial_hooks(fake, lambda prompt: next(answers)))
    assert code == 1
    assert "Expected exactly one servo on the bus, found ids 1, 2" in text and "Stopped." in text
    assert fake.get(1, 16, size=2) == 1000  # nothing was changed


def test_setup_motors_can_skip_modules(home):
    fake = FakeSerial((1,))
    answers = iter(["s", "s", "s", ""])
    code, text = run("setup-motors", "--port", "/dev/fake", hooks=serial_hooks(fake, lambda prompt: next(answers)))
    assert code == 0 and text.count("skipped") == 3
    assert list(fake.servos) == [4] and "slider: id 1 -> 4" in text


def test_setup_motors_refuses_the_simulator_and_a_cap_above_the_limit(capsys):
    assert run("setup-motors", "--sim")[0] == 2
    assert "already set up" in capsys.readouterr().err
    assert run("setup-motors", "--port", "/dev/fake", "--cap", "900")[0] == 2
    assert "--cap must be" in capsys.readouterr().err


def test_bench_calibration_is_saved_and_then_used_by_monitor(home):
    fake = FakeSerial((1, 2, 3, 4))
    fake.set(4, 56, 2300, size=2)
    hooks = serial_hooks(fake)

    code, text = run("monitor", "--port", "/dev/fake", "--once", hooks=hooks)
    assert code == 0 and "not calibrated" in text
    assert "slider     4   2300" in text and "value" not in text  # raw view

    code, text = run("calibrate", "--bench", "--port", "/dev/fake", hooks=hooks)
    assert code == 0
    saved = home / "calibration" / "bench.json"
    assert f"Saved to {saved}" in text and "slider     4     1932    2300      2668" in text
    calibration = Calibration.load(saved)
    assert calibration.bench and calibration.modules["slider"].raw_lo == 1932

    code, text = run("monitor", "--port", "/dev/fake", "--once", hooks=hooks)
    assert code == 0 and "calibration 'bench'" in text
    assert "3.00" in text.splitlines()[-1]  # the slider's present position reads as mid-scale


def test_bench_calibration_reports_a_horn_too_far_from_the_middle(home, capsys):
    fake = FakeSerial((1, 2, 3, 4))
    fake.set(2, 56, 300, size=2)
    code, _ = run("calibrate", "--bench", "--port", "/dev/fake", hooks=serial_hooks(fake))
    assert code == 1
    assert "dial: the horn is at 300" in capsys.readouterr().err
    assert not (home / "calibration" / "bench.json").exists()


def test_calibrate_without_bench_says_what_is_missing(capsys):
    assert run("calibrate", "--sim")[0] == 2
    assert "lebox calibrate --bench" in capsys.readouterr().err


def test_commands_that_need_a_calibration_say_how_to_make_one(home, capsys):
    code, _ = run("move", "--port", "/dev/fake", "slider=3", hooks=serial_hooks(FakeSerial((1, 2, 3, 4))))
    assert code == 1
    assert "lebox calibrate --bench --port /dev/fake" in capsys.readouterr().err


def test_move_on_a_real_bus_is_refused_until_the_cap_is_stored(home, capsys):
    fake = FakeSerial((1, 2, 3, 4))
    fake.motors = True
    hooks = serial_hooks(fake)
    assert run("calibrate", "--bench", "--port", "/dev/fake", hooks=hooks)[0] == 0
    code, _ = run("move", "--port", "/dev/fake", "slider=4", hooks=hooks)
    assert code == 1
    assert "stored torque cap of 100 %" in capsys.readouterr().err
    assert fake.get(4, 40) == 0 and fake.get(4, 48, size=2) == 1000  # torque never enabled, limit untouched


def test_move_on_a_real_bus_drives_capped_servos_and_releases_them(home):
    fake = capped_board()
    hooks = serial_hooks(fake)
    assert run("calibrate", "--bench", "--port", "/dev/fake", hooks=hooks)[0] == 0
    code, text = run("move", "--port", "/dev/fake", "slider=4", "switch=II", hooks=hooks)
    assert code == 0
    rows = {line.split()[0]: line for line in text.splitlines()[1:]}
    assert "4.00        4.00" in rows["slider"] and "+40.0 deg   +40.0 deg" in rows["switch"]
    assert all(fake.get(servo_id, 40) == 0 for servo_id in fake.servos)  # all released
    assert fake.get(4, 48, size=2) == 150  # the working limit, not the cap
    assert not fake.is_open


def test_a_real_servo_that_does_not_move_is_reported_as_stalled_and_released(home, capsys):
    fake = capped_board()
    fake.motors = False  # torque on, but nothing moves: a blocked horn
    hooks = serial_hooks(fake)
    assert run("calibrate", "--bench", "--port", "/dev/fake", hooks=hooks)[0] == 0
    code, _ = run("move", "--port", "/dev/fake", "slider=4", hooks=hooks)
    assert code == 1
    assert "stalled" in capsys.readouterr().err
    assert fake.get(4, 40) == 0


@pytest.mark.parametrize(
    "argv, status, message",
    [
        (["move", "--sim", "slider"], 2, "write module=position"),
        (["move", "--sim", "lever=3"], 2, "no module named lever"),
        (["move", "--sim", "slider=9"], 1, "outside the working range"),
        (["move", "--sim", "slider=3", "--torque", "900"], 1, "at most the allowed cap"),
        (["selftest", "--sim", "--n", "0"], 2, "--n must be"),
        (["selftest", "--sim", "--module", "lever"], 2, "no module named lever"),
        (["task", "run", "--sim", "nope"], 1, "neither a built-in task"),
        (["task", "run", "--sim", "slider_to_3", "--hz", "0"], 2, "--hz must be"),
    ],
)
def test_mistakes_on_the_command_line(argv, status, message, capsys):
    code, _ = run(*argv)
    assert code == status
    assert message in capsys.readouterr().err


def test_move_and_reset_on_the_simulated_board():
    code, text = run("move", "--sim", "slider=4", "dial=closed")
    assert code == 0 and text.splitlines()[0].split() == ["module", "target", "reached", "error", "counts", "time"]
    code, text = run("reset", "--sim", "dial_open", "--seed", "3")
    assert code == 0 and text.startswith("dial_open: start state in place, board passive.")
    assert run("reset", "--sim", "dial_open", "--seed", "3")[1] == text  # same seed, same start


def test_selftest_reports_per_module_and_saves_every_move(tmp_path):
    path = tmp_path / "selftest.json"
    code, text = run("selftest", "--sim", "--n", "3", "--module", "slider", "--module", "dial", "--json", str(path))
    assert code == 0
    assert "3 random targets per module" in text
    assert [line.split()[0] for line in text.splitlines()[2:4]] == ["slider", "dial"]
    records = json.loads(path.read_text())
    assert len(records) == 6 and set(records[0]) == {"module", "target", "reached", "error", "error_ticks", "seconds"}


def test_task_run_resets_scores_live_and_saves_the_trace(tmp_path):
    trace = tmp_path / "ep.jsonl"
    code, text = run("task", "run", "--sim", "slider_to_3", "--seed", "1", "--trace", str(trace))
    assert code == 0
    lines = text.splitlines()
    assert lines[0] == "slider_to_3: start state in place (slider at 2). Board passive."
    assert lines[1] == 'Instruction: "Move the slider to 3"'
    assert lines[2] == "slider_to_3: success"
    assert lines[-1] == f"Trace saved to {trace}"
    samples = load_trace(trace)
    assert samples[0].t == 0.0 and samples[0].values["slider"] == pytest.approx(2.0, abs=0.01)
    assert samples[-1].values["slider"] == pytest.approx(3.0, abs=0.15)
    # Scoring the saved trace again gives the same outcome.
    assert run("score", "slider_to_3", str(trace))[1].splitlines()[0] == "slider_to_3: success"


@pytest.mark.parametrize("task_name", ["switch_to_i", "dial_close", "plunger_pull", "slider_to_5", "dial_to_50"])
def test_the_simulated_hand_can_solve_the_builtin_tasks(task_name):
    code, text = run("task", "run", "--sim", task_name, "--seed", "5")
    assert code == 0 and f"{task_name}: success" in text


def test_task_run_with_an_idle_hand_runs_to_the_timeout():
    code, text = run("task", "run", "--sim", "switch_to_ii", "--seed", "2", "--sim-play", "idle")
    assert code == 0
    assert "switch_to_ii: no_contact" in text and "duration       30.0 s" in text


def test_task_run_without_reset_scores_from_the_present_state():
    code, text = run("task", "run", "--sim", "slider_to_1", "--no-reset")
    assert code == 0
    assert "scoring from where the modules are now" in text and "slider_to_1: success" in text


def test_ctrl_c_during_a_move_releases_torque_and_exits_cleanly(home, capsys):
    fake = capped_board()
    hooks = serial_hooks(fake)
    assert run("calibrate", "--bench", "--port", "/dev/fake", hooks=hooks)[0] == 0

    class Interrupting(InstantClock):
        def sleep(self, seconds):
            if fake.get(4, 40) == 1:  # torque is on: the move is under way
                raise KeyboardInterrupt
            super().sleep(seconds)

    hooks.clock = Interrupting()
    code, _ = run("move", "--port", "/dev/fake", "slider=5", hooks=hooks)
    assert code == 130
    assert "Torque is released" in capsys.readouterr().err
    assert fake.get(4, 40) == 0
