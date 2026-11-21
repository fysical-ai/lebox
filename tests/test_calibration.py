import json

import pytest

from lebox.calibration import Calibration, CalibrationError, ModuleCalibration, default_path
from lebox.modules import DEFAULT_MODULES, SLIDER


def test_design_calibration_is_centred_and_flagged_nominal():
    cal = Calibration.from_design()
    assert cal.nominal is True
    assert set(cal.modules) == {"switch", "dial", "plunger", "slider"}
    for spec in DEFAULT_MODULES:
        entry = cal.modules[spec.name]
        assert (entry.raw_lo + entry.raw_hi) / 2 == pytest.approx(2048, abs=1)
        assert entry.travel_ticks == pytest.approx(spec.expected_travel_ticks, abs=1)
    assert cal.problems() == []


def test_save_and_load_round_trip(tmp_path):
    cal = Calibration(
        board_id="bench-1",
        modules={"slider": ModuleCalibration(1700, 2435), "dial": ModuleCalibration(3600, 480)},
    )
    path = cal.save(tmp_path / "nested" / "bench-1.json")
    loaded = Calibration.load(path)
    assert loaded.board_id == "bench-1"
    assert loaded.modules == cal.modules
    assert loaded.created == cal.created
    assert loaded.nominal is False


def test_default_path_follows_lebox_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEBOX_HOME", str(tmp_path))
    assert default_path("bench-1") == tmp_path / "calibration" / "bench-1.json"
    cal = Calibration.from_design(board_id="bench-1")
    assert cal.save() == tmp_path / "calibration" / "bench-1.json"


@pytest.mark.parametrize("board_id", ["../escape", "a/b", "", "bench 1", ".hidden"])
def test_board_id_cannot_leave_the_calibration_folder(board_id):
    with pytest.raises(CalibrationError):
        default_path(board_id)


def test_problems_flags_a_false_stop():
    cal = Calibration.from_design()
    good = cal.modules["slider"]
    # Slider stopped 20 % short: a tight print or something in the slot.
    short = round(SLIDER.expected_travel_ticks * 0.8)
    cal.modules["slider"] = ModuleCalibration(good.raw_lo, good.raw_lo + short)
    problems = cal.problems()
    assert len(problems) == 1
    assert problems[0].startswith("slider: travel is")
    assert "-20%" in problems[0]


def test_problems_accepts_travel_within_tolerance():
    cal = Calibration.from_design()
    good = cal.modules["slider"]
    cal.modules["slider"] = ModuleCalibration(good.raw_lo, round(good.raw_lo + SLIDER.expected_travel_ticks * 1.03))
    assert cal.problems() == []


def test_problems_flags_missing_modules_and_out_of_range_readings():
    cal = Calibration(board_id="x", modules={"switch": ModuleCalibration(-10, 946)})
    problems = cal.problems()
    assert any(p.startswith("switch: low stop reading -10") for p in problems)
    assert "dial: not calibrated" in problems


def test_load_reports_a_missing_file(tmp_path):
    with pytest.raises(CalibrationError, match="no calibration file"):
        Calibration.load(tmp_path / "nope.json")


def test_load_reports_broken_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CalibrationError, match="not valid JSON"):
        Calibration.load(path)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"format": 1},
        {"format": 99, "board_id": "x", "modules": {}},
        {"format": 1, "board_id": "x", "modules": {"slider": {"raw_lo": 1}}},
        {"format": 1, "board_id": "x", "modules": {"slider": {"raw_lo": "a", "raw_hi": 2}}},
        {"format": 1, "board_id": "x", "modules": ["slider"]},
    ],
)
def test_load_reports_malformed_content(tmp_path, data):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CalibrationError):
        Calibration.load(path)
