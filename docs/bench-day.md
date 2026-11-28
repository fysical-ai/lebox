# Bench day: first run on real servos

The goal for the day: the whole loop running on loose servos, with no printed parts. The box puts a task's start state in place and releases, a hand turns the horn as the robot would, and the encoders score it live.

Nothing in this plan has been run on hardware yet. The driver in `src/lebox/feetech.py` has been tested against an emulated servo only, so the steps go from harmless to less harmless: read only first, then permanent settings, then torque on one servo, then all four.

If you are following this with Claude Code, work through the steps in order and stop at any step whose "pass" line is not met.

## Rules for the day

- Supply: 5 V. Not more.
- The first time torque goes on, have one servo connected, its body taped or clamped to the desk, the horn free, and a hand on the power plug.
- Never raise `--torque` above the stored cap (300 per mille = 30 %). `lebox` refuses anyway.
- If anything behaves oddly, pull the power, not the USB cable. With power gone the servos are limp.

## What you need

- 4 x STS3215 servos, each with a horn fitted and a tape flag on the horn so you can see it turn. Number them 1 to 4 with tape.
- The bus servo adapter and a USB data cable. If the adapter has a mode jumper, it must be in the USB position.
- 5 V supply, 3 A or more, 5.5 x 2.1 mm plug.
- Ubuntu: your user in the `dialout` group (`sudo usermod -aG dialout $USER`, then log out and in).

Which servo plays which module:

| ID | Module | Scale | Horn rotation |
|---|---|---|---|
| 1 | switch | -40 deg (I), 0 (O), +40 deg (II) | 1 deg per degree |
| 2 | dial | 0 % (CLOSED) to 100 % (OPEN) | 2.7 deg per 1 % |
| 3 | plunger | 0 to 4 | 8.3 deg per unit |
| 4 | slider | 1 to 5 | 15.9 deg per unit |

## Step 0. Install (no hardware)

```bash
git clone https://github.com/fysical-ai/lebox && cd lebox
python3 -m venv .venv && source .venv/bin/activate     # sudo apt install python3-venv if this fails
pip install -e ".[dev]"
pytest
lebox task run --sim slider_to_3
```

Pass: all tests pass and the last command prints `slider_to_3: success`.

## Step 1. Find the adapter

Connect one servo to the adapter, then power, then USB.

```bash
lebox ports
export PORT=/dev/ttyACM0        # whatever the line above showed
```

Pass: a port is listed.

## Step 2. Read only

```bash
lebox ping --port $PORT
lebox monitor --port $PORT
```

`ping` sends nothing but pings and reads. Expect one row: model 777, about 5 V, room temperature, torque off, limit 100 %, cap 100 %. `monitor` shows the raw encoder count (no calibration exists yet); turn the horn by hand and watch it change.

Pass: steady readings between 0 and 4095 that follow your hand.

Write down: the model number, the voltage, and which way the count goes up when you turn the horn clockwise seen from the horn side.

If the model is not 777, stop: the register table in the driver may not fit this servo.

## Step 3. IDs and the torque cap (permanent settings)

```bash
lebox setup-motors --port $PORT
```

It asks for one servo at a time, gives each its id (1 switch, 2 dial, 3 plunger, 4 slider) and stores a torque cap of 30 % in the servo's own memory. Then switch the servo power off and on, chain all four together, and:

```bash
lebox ping --port $PORT
```

Pass: four rows, ids 1 to 4, and after the power cycle the limit column reads 30 %. That is the servo starting up already capped, with no software involved.

## Step 4. Is the cap a ceiling or only a starting value? (torque stays off)

The Feetech documentation says the stored maximum torque is copied to the working limit at power-on. It does not say whether the servo also refuses a higher working limit later. Find out:

```bash
python - <<'PY'
import os
from lebox.feetech import FeetechBus
bus = FeetechBus(os.environ["PORT"])
bus.write_torque_limit(1, 800)
print("cap", bus.read_torque_cap(1), "limit after writing 800:", bus.read_torque_limit(1))
bus.close()
PY
```

Then switch the servo power off and on again, so the limit is back at the cap.

Either answer lets you continue, because `lebox` never asks for more than the cap and reads the limit back before every move. Write down which it was: 300 means the cap is a hard ceiling; 800 means it only sets the starting value, and the safety case rests on the software checks plus the capped start.

## Step 5. First motion: one servo

Leave only servo 4 (slider) connected for this step, or keep the others well clear. Turn every connected horn to roughly the middle of its range first: bench calibration takes "where the horn is now" as mid-travel.

```bash
lebox calibrate --bench --port $PORT
lebox move --port $PORT slider=4
lebox move --port $PORT slider=2
```

Expect a slow, steady creep to the position, then the horn goes limp. If it reports "stalled ... the torque limit is too low", repeat with `--torque 200`, then `--torque 250`.

Now the stall test. Hold the horn firmly and run:

```bash
lebox move --port $PORT slider=5
```

Pass: the move gives up within about half a second, prints "stalled ... torque released", and the horn is limp in your fingers. The push you felt should be gentle.

Write down: the lowest `--torque` that moves the servo every time, and how the stall felt.

## Step 6. All four, and repeatability

With all four connected and calibrated (run `lebox calibrate --bench` again now that all horns are mid-travel):

```bash
lebox move --port $PORT switch=II dial=open plunger=4 slider=1
lebox selftest --port $PORT --n 50 --json selftest-bench.json
```

Pass: every move ends within a few counts of its target, and no stalls.

Write down: the selftest table. These are the first repeatability numbers. They describe bare servos; the printed mechanisms will add gear backlash.

## Step 7. The whole loop

```bash
lebox task run --port $PORT slider_to_3 --trace traces/slider_to_3.jsonl
```

The box drives the slider servo to a start position and releases it. The live line shows where the slider is on its 1 to 5 scale. Turn the horn until it reads 3.00 and hold it there for a second.

Then break it on purpose, one run each:

| What you do | Expected outcome |
|---|---|
| Nothing for 30 s | `no_contact` |
| Turn the dial servo instead | `wrong_module` |
| Sweep the slider through 3 without stopping, on to the far end | `overshoot` |
| Move it part of the way towards 3 and stop short | `partial` |
| Reach 3, and also turn the switch servo and leave it | `side_effect` |
| Ctrl+C part way | `aborted` |

Then run every built-in task once (two of them have several steps):

```bash
for task in $(lebox task list | awk '{print $1}'); do
  lebox task run --port $PORT $task --trace traces/$task.jsonl
done
```

Pass: every outcome matches what your hand did.

## If something goes wrong

| Symptom | Likely cause |
|---|---|
| `cannot open /dev/ttyACM0 ... Permission denied` | Not in the `dialout` group yet, or not logged out and in since |
| `lebox ports` lists nothing | Charge-only USB cable, or the adapter's mode jumper (if it has one) is not in the USB position |
| `No servo answered` | Servo power off, servo cable not seated, or the servo is not at 1 Mbit/s |
| Position reads the same odd number whatever you do | The adapter may be echoing what is sent; the driver does not handle that yet |
| `stored torque cap of 100 %` | Step 3 has not been done for that servo |
| `stalled` with nothing blocking the horn | Torque limit too low for that servo's friction: raise `--torque` a step |
| `the horn is at ...` during calibration | Turn that horn towards mid-range (about 2048 in `lebox monitor`) and calibrate again |

## Not part of this day

- Finding the real hard stops (needs the printed parts).
- Approaching every target from the same side to cancel gear backlash.
- Recording and policy runs with LeRobot (needs the arm).

## Results

Fill in as you go.

| Item | Result |
|---|---|
| Model number read | |
| Supply voltage read | |
| Count direction for clockwise | |
| Limit after power cycle (step 3) | |
| Limit after writing 800 (step 4) | |
| Lowest reliable `--torque` per servo | |
| Stall release, feel and time | |
| Selftest: mean and max error per module | |
| Outcomes that did not match | |
