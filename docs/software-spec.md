# LeBox software spec v0.1 (21 Nov 2026)

Status: high-level design agreed. Steps 1 to 3 of the build order are implemented and run end to end on a simulated board. The servo driver is tested against an emulated servo only; nothing has run on hardware yet. `docs/bench-day.md` is the plan for the first hardware run.

## Decisions
- The policy never sees box state. Its only inputs are the sensors (cameras, arm state). Box encoder readings are logged alongside each frame for scoring, not as observations.
- Calibration is automatic by default: each servo finds its own hard stops by stall detection, with torque as low as possible so printed parts cannot be snapped. A manual sweep stays as fallback.
- LeRobot version to target: not chosen yet. `lerobot-rollout` and strategy plugins need v0.6.0 or later.

## Architecture
One small library with no knowledge of policies, plus two thin adapters into LeRobot.

| Piece | Job |
|---|---|
| `Board` | List of module descriptors (servo ID, scale, markings). Reads every module in real units; passive (torque off) and drive modes. Talks to hardware only through the `ServoBus` interface; own USB adapter, IDs 1-4. |
| Calibration | Finds stops, sets encoder offset, saves one JSON per board id. |
| Reset | Drives modules to a start state and verifies it. |
| Tasks | Declarative YAML: instruction, start, goal, tolerance, hold time, timeout. |
| Scorer | Pure function of task + encoder trace -> outcome and metrics. |
| `lerobot_strategy_lebox` | Rollout strategy plugin for `lerobot-rollout --strategy.type=lebox`. Episodic loop with the reset timer replaced by a box reset, plus scoring. Box state goes in strategy-owned dataset columns. |
| `lebox-record` | Own teleop recording script on LeRobot's robot, teleoperator and dataset classes (`lerobot-record` has no plugin hook). |
| Report | Success rate and failure breakdown per checkpoint; later an overnight sweep. |

Units shown to the user: switch I / O / II (degrees), dial 0-100 % (0 % at the clockwise stop, CLOSED), plunger 0-4, slider 1-5.

## Calibration routine and its safety rules
Servo: STS3215 C046, listed stall torque 14.4 kg.cm at 7.4 V and 9.2 kg.cm at 6 V. LeBox runs at 5 V; no listed figure, estimate 7 to 8 kg.cm. At full stall that is about 2 kg at the slider and plunger gears (36 and 34.5 mm radius), inside the 3 kg load the parts were sized against, but the dial and switch are direct drive and have no stress number for their stops yet.

Rules:
1. Hard cap in the servo itself: write `Max_Torque_Limit` (EEPROM) during `lebox setup-motors`, so no software fault or power cycle can exceed it. Level set after measuring, about twice the minimum moving torque. To verify on one loose servo: that the cap survives a power cycle and that the working limit starts from it.
2. Find the minimum torque: start near 5 %, raise in small steps until the element just moves; calibrate at that plus a small margin. Store per module as a friction baseline.
3. Creep, never jump: goal only 1 to 2 degrees ahead of the present position at low speed. Small arrival energy, and no long way round if the encoder wrap lies inside the travel.
4. Stop = position unchanged for about 0.2 s. Release torque at once and back off. Never sit in stall.
5. Read back every limit register before enabling torque; refuse to move on a mismatch.
6. Compare measured travel with the design travel (switch 84 deg, dial 274 deg, plunger 20 mm, slider 40.6 mm); reject if more than about 5 % off. Catches a false stop on a tight print.
7. Working range = stops minus a margin (switch +-40 of +-42 deg, dial +-135 of +-137 deg).
8. First runs: one bare servo on the desk, then one module at a time with a hand on the power plug.

To do before the first powered run: stress estimate from CAD for the dial stop lug and the switch slot ends at the capped torque.

## Tasks and success
The task format and the scoring rules are described in full in `docs/tasks.md`. In short: a task is an ordered list of reach and hold steps with relative time windows; the robot gets the whole instruction up front; the steps are the definition and the wording is a separate layer; a standard description is generated from the steps. What follows in this section is the original one-step design, which is now the one-step case.

Three layers: state (raw encoders, logged every frame), task (written once), outcome (computed, never hand-labelled). Any episode can be re-scored later under a new definition.

Outcomes: `success`, `late`, `side_effect`, `overshoot`, `partial`, `wrong_module`, `no_contact`, `aborted`. Metrics: progress (0-1, at the end and best), final error per goal module, time to goal, duration, which modules moved and which were left disturbed.

Rules as implemented:
- Success needs every goal module inside its goal and, with `others: unchanged`, every other module within tolerance of where it started, continuously for `hold_s`. The live scorer ends the episode at that moment; otherwise the episode runs to `timeout_s` and is classified from its final state.
- `late` is new against the first draft of this spec: in the goal when the episode ended, but not held long enough.
- A module counts as moved beyond a quarter of its marking tolerance, and as disturbed beyond the full tolerance (switch 5 deg, dial 3 %, plunger and slider 0.15) unless `others_tol` says otherwise.
- The start state for scoring is the first sample of the trace, i.e. where the modules really were.
- Task files: `start` takes a value or marking, `{sample: [...]}`, `{range: [low, high]}` or `random`; `goal` takes a value or marking, `{target, tol}` or `{min, max}`. Every goal module must have a start, and a start may not lie in or next to the goal.
- Not yet supported: goals sampled per episode ("move the slider to {n}"), and ordered multi-step tasks.

Example task:
```yaml
name: slider_to_3
instruction: "Move the slider to 3"
start:
  slider: {sample: [1, 2, 4, 5]}
  dial: 50
goal:
  slider: {target: 3, tol: 0.15}
others: unchanged
hold_s: 1.0
timeout_s: 30
```

## Driver and motion, as implemented
- `feetech.py` speaks the Feetech serial protocol directly through pyserial at 1 Mbit/s, so it does not depend on a LeRobot version. Registers used: model (3), id (5), stored maximum torque (16), torque switch (40), goal position (42), working torque limit (48), lock (55), present position (56), voltage (62), temperature (63).
- The Feetech memory table says the stored maximum torque is copied to the working limit at power-on. Whether the servo also refuses a higher working limit afterwards is not documented; bench-day step 4 tests it.
- `motion.py` is the only code that enables torque. Per move: refuse unless the stored cap is at or below 40 %; write the working limit (default 15 %) and read it back; enable torque with the goal on the present position; advance the goal at 800 counts/s (about 70 deg/s), never more than 60 counts ahead of the servo; no progress for 0.25 s is a stall and releases at once, unless the servo is at rest within 25 counts of the target, which counts as arrived and is reported as the error; torque is always off at the end.
- `lebox setup-motors` assigns ids 1-4 one servo at a time and stores a 30 % cap.
- `lebox calibrate --bench` is for loose servos without stops: the present position becomes mid-travel and the design travel sets the ends.
- Not yet done from the rules above: finding the stops by stall, finding the minimum torque automatically, comparing measured with design travel on a real board, and approaching from the same side.
- Possibly useful later: writing 128 to the torque switch makes the present position read 2048, which would centre a module's travel on the encoder.

## Reset
The start state belongs to the task (fixed or sampled); `lebox home set` captures a default base environment. Sequence: confirm arm parked clear -> torque on, capped -> drive each module to target, always approaching from the same side -> verify -> torque off -> confirm it stayed -> log actual start. A stall during reset stops and flags the episode.

## Commands
`lebox setup-motors`, `lebox calibrate`, `lebox monitor`, `lebox move`, `lebox reset`, `lebox home set`, `lebox selftest`, `lebox task list|check`, `lebox-record`, `lerobot-rollout --strategy.type=lebox`, `lebox report`.

## Build order
1. `Board` + `monitor`, plus a simulated board for development without hardware. **Done, simulated board only.**
2. Feetech bus driver, then `calibrate`, `move`, `reset`, `selftest` (reset repeatability is a paper result). **Written and passing on the simulator; first hardware run pending.** Calibration so far is the bench version for loose servos; finding the printed hard stops is still to do.
3. Tasks + scorer. **Done on the simulated board**; testing by hand on the real box follows the driver.
4. `lebox-record`.
5. Rollout strategy.
6. `report`, then the overnight sweep over checkpoints.

Steps 1-3 need neither the arm nor a policy.

## Open items
1. Pick and pin the LeRobot version; decide whether the bus driver wraps LeRobot's motor bus class or the Feetech SDK directly.
2. Confirm in the first test that strategy-owned dataset columns stay out of the policy's inputs.
3. Confirm the Feetech register behaviour in safety rule 1 on a loose servo.
4. Stress estimate for dial lug and switch stops (see above).
5. Whether success should also require the arm to have let go or returned to park.
6. Plunger scale: the code assumes ring 0 shows when fully pushed in and ring 4 when fully pulled out, on the stops. To confirm on the printed rod.
7. Which physical end of the switch and slider is the low end of the scale is set by calibration, not assumed.
