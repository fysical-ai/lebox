# LeBox

An open-source, self-resetting, self-scoring test bench for robot manipulation policies.

Evaluating a policy on a real robot usually needs a person to put the scene back after every episode and to judge whether the episode worked. LeBox removes both jobs. It is a 2 x 2 board of four modules: a knife switch, a valve handle, a push-pull plunger and a slider. Each module is coupled to one Feetech STS3215 bus servo that does three things:

- **torque off:** a passive, back-drivable joint the robot operates
- **encoder:** ground-truth state for scoring, with no camera or classifier
- **torque on:** resets the module to any commanded or randomised start state

LeBox is built for [LeRobot](https://github.com/huggingface/lerobot) and the SO-101 arm.

## Status

Early. This repository currently holds the first piece of the host software, and it runs against a simulated board only.

| Piece | State |
|---|---|
| Module descriptors and unit scales | done |
| Calibration file (save, load, sanity check) | done |
| Board: read every module in real units | done |
| Simulated servo bus | done |
| `lebox monitor --sim` | done |
| Feetech bus driver | not started; needs the hardware |
| `lebox calibrate`, `move`, `reset`, `selftest` | not started |
| Tasks and scorer | not started |
| `lebox-record` and the `lerobot-rollout` strategy | not started |

The design is in [docs/software-spec.md](docs/software-spec.md).

## Try it

Python 3.10 or newer. No dependencies.

```bash
pip install -e ".[dev]"
lebox monitor --sim
```

```text
LeBox monitor: simulated board, design-value calibration
module    id    raw       value  at      low stop to high stop
switch     1   2442   +34.6 deg  -       |----------------------o--|
dial       2   3083      83.7 %  -       |--------------------o----|
plunger    3   2145        3.03  3       |------------------o------|
slider     4   2211        3.90  4       |-----------------o-------|
```

`--once` prints a single reading; `--frames N` prints N. Without `--sim` the command stops with a message, because the driver for the real bus is not written yet.

Run the tests with `pytest`.

## Scales

Each module reads in the units printed on its tile.

| Module | Servo ID | Scale | Markings |
|---|---|---|---|
| switch | 1 | degrees, -40 to +40 | I, O, II |
| dial | 2 | percent open, 0 to 100 | CLOSED, 25, 50, 75, OPEN |
| plunger | 3 | ring number, 0 to 4 | 0 to 4 |
| slider | 4 | mark number, 1 to 5 | 1 to 5 |

The scale is linear between the two hard stops. The software commands and scores in a working range slightly inside the stops.

## Using it from Python

```python
from lebox import Board, Calibration, SimBus

calibration = Calibration.from_design()      # design values, for simulation only
bus = SimBus.for_board(calibration, start={"slider": 2.0})
board = Board(bus, calibration)

board.passive()                              # torque off: modules can be moved
values = {name: round(value, 2) for name, value in board.values().items()}
print(values)                                # {'switch': 0.0, 'dial': 50.0, 'plunger': 2.0, 'slider': 2.0}
print(board.read()["slider"].label)          # 2
```

## Licence

MIT. The LeBox hardware builds on Microsoft's BusyBox (MIT) and on the SO-ARM100 (Apache-2.0); their licence texts ship with the hardware files.
