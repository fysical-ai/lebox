# Tasks

How a task is defined, worded and scored.

## The idea

A task is an ordered list of steps over the board's state. One step makes a short task; many make a long-horizon one. The format is the same.

Each step is one of two kinds:

| Kind | Meaning | Example |
|---|---|---|
| reach | bring modules into a goal region: a target and a tolerance | "turn the blue valve to 50%" |
| hold | leave everything as it is for a set time | "wait 30 seconds" |

The robot always gets the whole instruction up front. Nothing tells it when a step is done, so in a hold step it is the robot that has to keep time.

## Terms

| Term | Meaning |
|---|---|
| Task | The specification: start positions, steps, rules. Written once. |
| Episode | One attempt at a task, from reset to outcome. Also called a rollout or trial. |
| Step | One subgoal of the task. Also called a subtask or stage. |
| State | The position of every module, read from the encoders. Ground truth: used to score, never shown to the policy. |
| Goal region | A target position with a tolerance either side. |
| Time window | How long a reach may take, or how long a hold lasts. |
| Instruction | The words the policy is given. |
| Standard description | The task written in a fixed notation generated from its steps. |
| Board | The modules and their servo ids, names and scales. Run `lebox board` to see it. |

Tasks name modules (`dial`), not servos. The board maps each module to its servo id, so a task stays valid on another box whose ids differ.

## The steps are the definition

"Turn the blue dial to 0%" and "close the blue valve" are the same task, because they come down to the same step: dial at 0, within tolerance. So a task has two layers.

- **Steps** say what must happen. They are exact and machine-checkable.
- **Wording** is what the policy is told. A step can carry several wordings, and one is picked per episode.

From the steps, LeBox generates two things, so nobody has to write them:

- the **standard description**, e.g. `dial=50±3 within 20s | hold 30s (-5s) | slider=5±0.15 within 20s`. It is the same whatever the wording, so results from differently worded episodes of one task can be grouped.
- a **plain sentence** for any step that has no wording, e.g. "Set the blue valve to 50%".

## Writing a task

```yaml
name: valve_wait_slider
start:
  dial: {range: [0, 30]}
  slider: {sample: [1, 2]}
steps:
  - say: ["Turn the blue valve to 50%", "Set the blue valve half open"]
    reach: {dial: 50}
    within_s: 20
  - say: ["Wait 30 seconds", "Pause for half a minute"]
    hold_s: 30
    early_s: 5
  - say: ["Slide the slider to 5", "Move the white slider all the way to 5"]
    reach: {slider: 5}
    within_s: 20
```

A one-step task can use the short form, with `goal` in place of `steps`:

```yaml
name: slider_to_3
instruction: "Move the slider to 3"
start:
  slider: {sample: [1, 2, 4, 5]}
goal:
  slider: {target: 3, tol: 0.15}
```

### Start

Where modules are put before each episode. Modules left out stay where they are.

| Form | Meaning |
|---|---|
| `dial: 50` or `switch: II` | this position or marking |
| `slider: {sample: [1, 2, 4, 5]}` | one of these, picked per episode |
| `dial: {range: [0, 30]}` | anywhere in between |
| `dial: random` | anywhere in the working range |

Every module that a step names needs a start, and the start is kept clear of that module's first goal, so no episode begins already solved.

### Goals in a reach step

| Form | Meaning |
|---|---|
| `dial: 50` or `switch: II` | this position, with the module's usual tolerance |
| `slider: {target: 3, tol: 0.15}` | this position, with this tolerance |
| `dial: {min: 75}`, `dial: {max: 25}`, `dial: {min: 40, max: 60}` | anywhere in this band |
| `switch: start` | wherever the module was when the episode began ("put it back") |

Several modules in one step mean all of them at once. The usual tolerances are 5 degrees for the switch, 3 % for the dial, 0.15 of a mark (1.5 mm) for the slider and half a ring (2.5 mm) for the plunger.

### Times

All times are relative.

| Setting | On | Meaning |
|---|---|---|
| `within_s` | reach | time allowed, counted from the moment the step before was reached |
| `hold_s` | hold | how long nothing may move |
| `early_s` | hold | how much early the robot may move on (default 0) |
| `timeout_s` | task | the whole episode; by default the sum of the steps plus time to confirm them |
| `hold_s` | task | how long the final goal must hold (default 1 s) |
| `settle_s` | task | how long an earlier step's goal must hold before the next step counts as begun (default 0.5 s) |

A step with no `within_s` has 30 s in the default timeout and no deadline of its own.

How late the robot may act after a wait is set by the next step's `within_s`.

### Wording

- `say` on a step: one sentence or a list of alternatives.
- `instruction` on the task: one or more wordings of the whole task. If given, it is used as it stands.
- Otherwise the instruction is composed from the steps in order: "Turn the blue valve to 50%, then wait 30 seconds, then slide the slider to 5."

### Other rules

- `others: unchanged` (the default): a module that no step has named yet must stay where it started. `others: ignore` lifts that.
- `others_tol: {dial: 10}` sets how far such a module may drift.

## Writing a task as a table

A task can also be a table with one row per step, which suits spreadsheets and machine-generated tasks.

| kind | say | switch | dial | plunger | slider | within_s | hold_s | early_s |
|---|---|---|---|---|---|---|---|---|
| start | | | 0..30 | | 1\|2 | | | |
| reach | Turn the blue valve to 50% | | 50 | | | 20 | | |
| hold | Wait 30 seconds | | | | | | 30 | 5 |
| reach | Slide the slider to 5 | | | | 5 | 20 | | |

An empty module cell means "leave it as it is". In a `start` row, `0..30` is a range and `1|2` a choice. In a `reach` row a cell is a position, a marking, `start`, `>=75` or `<=25`; a `tol_<module>` column sets a tolerance. Several wordings go in one `say` cell separated by `|`.

```bash
lebox task import my_task.csv -o my_task.yaml
```

## Step codes: a task on one line

Every standard step has a short code: the tile letter and the number printed on the tile.

| Tile | Module | Codes | Meaning |
|---|---|---|---|
| A | switch | `A0` `A1` `A2` | O, I, II (the printed symbols read as 0, 1, 2) |
| B | valve | `B0` `B25` `B50` `B75` `B100` | CLOSED, 25, 50, 75, OPEN |
| C | plunger | `C0` `C2` `C4` | pushed in, half way, pulled out (rings 0, 2 and 4) |
| D | slider | `D1` to `D5` | marks 1 to 5 |

These sixteen are the standard states. The valve, plunger and slider also take values in between (`B37`, `C1`, `D2.5`); those are legal but not standard.

The plunger has five rings but three standard states. The rings are 5 mm apart, which is too fine to ask an arm to pick one out, so the standard steps are in, half way and out, and a plunger goal allows half a ring either side.

| Code | Meaning |
|---|---|
| `W30` | wait 30 seconds |
| `AS` | put tile A back where it started |
| `B50+D5` | both in the same step |

A task is its steps in order, with an optional start state before a `>`:

```text
A1 B100 C4 D1 > C0 A2 D4 AS B0
```

A tile that the steps use but the start does not mention begins at one of its other standard states, picked per episode.

What a code does not say is fixed by the library, so the same line always makes the same task:

- the tile's usual tolerance;
- 20 s for each action;
- a wait may be left up to 10 % early (`W30` from 27 s on);
- one plain sentence and two alternatives for every standard step. `lebox codes` lists them.

A line of codes works wherever a task name does:

```bash
lebox task show "B50 W30 D5"
lebox task run --sim "A1 B100 C4 D1 > C0 A2 D4 AS B0"
lebox task build "B50 W30 D5" -o my_task.yaml     # write it out as a task file to edit
```

Letters name the kind of tile, not its place on the board: A is the knife switch wherever it sits. A new kind of tile gets the next letter.

## The standard test

The LeBox Standard Test is a fixed set of tasks for comparing policies. It is frozen and versioned: the tasks, the start states and the wordings all follow from the file and its seed, so two runs of the same version are the same test. It lives in `src/lebox/library/standard_test_v1.yaml`.

| Level | What it is | Sub-scores | Tasks |
|---|---|---|---|
| `single` | one action | to an end stop (8), to a middle mark (8), large valve turn (2) | 18 |
| `short` | two or three actions | pairs (4), triples (4) | 8 |
| `long` | five or more steps | a tile used twice or put back (6), with waits (4) | 10 |
| `control` | touch nothing for 10 s | | 1 |

The single actions between them reach every one of the sixteen standard states. The sub-scores come from the same run: they show whether single actions fail on precision, and whether long tasks fail on timing.

Each task runs 10 times, 370 episodes in all. Episode k of a task always has the same start state and the same wording.

```bash
lebox suite list                                   # the levels and tasks
lebox suite run --sim --out runs/demo              # the whole test on the simulator
lebox suite run --port /dev/ttyACM0 --out runs/1   # on a real box
lebox suite run --sim --out runs/q --episodes 1    # a quick pass: one episode per task
lebox suite report runs/1                          # print the summary of an earlier run
```

Options: `--level single` runs one level; `--wording varied` gives each episode one of the alternative wordings instead of the plain sentence.

The report gives, per level and sub-score: episodes, successes, the success rate with its 95 % interval, and for longer tasks the average number of steps completed. Every episode is saved to `results.jsonl` with its trace, so a run can be re-scored or re-reported later.

At the time limits the full test takes at most about five and a half hours; a policy that works finishes much sooner, because an episode ends as soon as the task is done.

The order of difficulty between the levels is a design assumption. It has not yet been checked against a trained policy.

## How steps are scored

The scorer works through the steps in order, from the encoder readings alone.

- **A reach step is done** when its goal has been reached and has held for `settle_s` (or the task's `hold_s` for the last step). At the moment the goal is reached, everything else must be in place: what earlier steps set must still be set, and modules that only later steps name must not have been moved yet. That keeps the steps in order.
- **The robot may start on the next step straight away.** It does not have to wait for the scorer to confirm the last one.
- **A hold step is done** when nothing has moved for `hold_s`, counted from the moment the step before was reached. Moving on up to `early_s` before the end is accepted. Moving on sooner ends the episode.
- **Finished steps must stay finished** until a later step names the same module again.
- **The episode ends** on success, when a reach step misses its `within_s`, when a wait is broken, or at `timeout_s`.

| Outcome | Meaning |
|---|---|
| `success` | every step done, the last goal held, nothing else disturbed |
| `late` | in the goal when the episode ended, but not yet held long enough |
| `side_effect` | goal reached, but another module was moved and left displaced |
| `overshoot` | the goal was reached at some point but not held |
| `partial` | a goal module was moved, the goal was never reached |
| `wrong_module` | only modules that are not part of the goal were moved |
| `wrong_order` | a later step was carried out before the present one |
| `too_early` | the robot moved on before a wait was over |
| `no_contact` | nothing moved |
| `aborted` | the episode was stopped from outside |

For a task with several steps, the outcome describes the step the episode stopped at. The result also gives the number of steps completed, the time each was reached, and progress from 0 to 1 with every step counting equally.

## Limits

- The box sees its own modules, not the arm. During a wait it can tell that nothing on the board moved; it cannot tell whether the arm stood still.
- Steps are strictly ordered. A checklist that may be done in any order is not supported.
- A goal cannot yet vary per episode ("move the slider to {n}" with a different n each time). Start positions can.
- The step codes assume the plunger's ring 0 shows when it is pushed fully in. That is still to be confirmed on the printed part.
