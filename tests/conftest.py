"""Shared test helpers: play scripted episodes on the simulated board."""

from __future__ import annotations

import pytest

from lebox import Board, Calibration, Sample, Scorer, SimBus
from lebox.modules import DEFAULT_MODULES

MID = {"switch": 0.0, "dial": 50.0, "plunger": 2.0, "slider": 1.0}


class Episode:
    """A simulated board whose modules follow scripted straight-line moves."""

    def __init__(self, start=None, noise_ticks=0.0):
        self.calibration = Calibration.from_design()
        self.start = {**MID, **(start or {})}
        self.bus = SimBus.for_board(self.calibration, start=self.start, noise_ticks=noise_ticks, seed=7)
        self.board = Board(self.bus, self.calibration)
        self.specs = {spec.name: spec for spec in DEFAULT_MODULES}

    def _place(self, module, value):
        spec = self.specs[module]
        cal = self.calibration.modules[module]
        self.bus.place(spec.servo_id, spec.raw_from_value(value, cal.raw_lo, cal.raw_hi))

    def samples(self, moves=(), duration=40.0, hz=10):
        """Board samples while ``moves`` play: (t_from, t_to, module, to_value) ramps, in order."""
        position = dict(self.start)
        origin = {}
        result = []
        for step in range(int(duration * hz) + 1):
            t = step / hz
            for index, (t_from, t_to, module, to_value) in enumerate(moves):
                if t < t_from:
                    continue
                origin.setdefault(index, position[module])
                fraction = 1.0 if t >= t_to else (t - t_from) / (t_to - t_from)
                position[module] = origin[index] + (to_value - origin[index]) * fraction
                self._place(module, position[module])
            result.append(Sample(t, self.board.values()))
        return result

    def play(self, task, moves=(), duration=40.0, hz=10):
        """Run the moves through a live scorer, stopping when it says the episode is over."""
        scorer = Scorer(task)
        for sample in self.samples(moves, duration, hz):
            if scorer.update(sample.t, sample.values):
                break
        return scorer.result()


@pytest.fixture
def episode():
    return Episode
