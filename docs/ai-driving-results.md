# AI driving bake-off: results

Models on Synthetic (Standard plan), all through the same driver and tasks. See `docs/ai-driving.md`.

## Rounds 1 and 2: shaking out the test (2026-10-25)

These rounds mainly found problems with the **test**, each fixed before the next round:

| Found | Effect | Fix |
|---|---|---|
| Head camera can't see the floor within ~2.5 m | Models lost sight of the ball when close | Added the floor camera (the real Beam+ has one), head camera tilted 8° down |
| 2,000-token reply limit | Thinking models (Qwen, GLM) were cut off mid-thought, wasting steps (~35 s each) | 8,000 tokens; cut-off steps reported back to the model |
| 7-minute time limit | Slow thinkers failed on time, not on skill | 15 minutes (the 20-step limit is the real budget) |
| Floor camera drew walls as a grey checker pattern | Read as "kitchen tiles"; models drove at walls | Plain dark walls; real per-room floors (kitchen tiles, bedroom carpet, wood) |
| 90 s request timeout | GLM's long thinks failed as errors | 180 s |
| 1.2 m "close enough" | Qwen failed at 1.24 m while facing the ball | 1.3 m (the task says "about 1 m") |

Round 2 (2 runs per task, before the floor and timeout fixes):

| model | ball | kitchen | bed | success | avg steps | avg time | bumps | cost |
|---|---|---|---|---|---|---|---|---|
| DeepSeek-V4.1-Flash | ✅ ✅ | ❌ ❌ | ❌ ❌ | 2/6 | 16.0 | 264 s | 15 | $0.041 |
| DeepSeek-V4.1-Flash-1017 | ✅ ✅ | ❌ ❌ | ❌ ❌ | 2/6 | 15.0 | 176 s | 16 | $0.028 |
| Qwen3.8-27B | ❌ ❌ | ❌ ❌ | ❌ ✅ | 1/6 | 17.0 | 471 s | 10 | $0.150 |
| GLM-5.3-Flash | ❌ ✅ | ❌ ❌ | ✅ ✅ | 3/6 | 14.2 | 540 s | 13 | $0.031 |

Patterns already visible:
- **DeepSeek (both versions):** fast (2–5 s per step) and excellent when the target is in view: the
  ball in 7–9 steps, every time. Weak at exploring: it spins in place scanning and doesn't keep track of
  where it's been, so the out-of-sight targets (bed, kitchen) run out of steps.
- **GLM-5.3-Flash:** the best explorer, finding the bed in 8–10 steps. But it thinks a long time (often
  20–80 s per step), so a single task takes many minutes.
- **Qwen3.8-27B:** slowest and the most expensive per task, with mixed results.
- **Everyone:** misjudges distances early. A 1.5 m drive from the start is "through the doorway" in the
  model's mind, but the doorway is 2.5 m away.

## Round 3: the real comparison (2026-10-25, all fixes in)

4 models × 3 tasks × 2 runs, 20 steps max each. Raw results are on the VM in
`~/beam-brain/bakeoff/20261025-174757/`: `results.json`, a map of every trial's path, and
`scripts/bakeoff-log.py` for step-by-step logs.

| model | ball | kitchen | bed | success | avg time per task | per step (think) | bumps | cut-off steps | cost (6 tasks) |
|---|---|---|---|---|---|---|---|---|---|
| DeepSeek-V4.1-Flash | ✅ ✅ | ❌ ❌ | ❌ ❌ | 2/6 | 4.4 min | ~3–5 s | 24 | 3 | $0.039 |
| DeepSeek-V4.1-Flash-1017 (beta) | ✅ ✅ | ❌ ❌ | ❌ ❌ | 2/6 | **3.4 min** | ~3–5 s | 19 | 1 | **$0.030** |
| Qwen3.8-27B | ✅ ✅ | ❌ ❌ | ❌ ✅ | **3/6** | 7.6 min | ~10–40 s | **6** | 2 | $0.186 |
| GLM-5.3-Flash | ✅ ✅ | ❌ ❌ | ❌ ❌ | 2/6 | 9.0 min | ~10–135 s | 18 | **12** | $0.055 |

### What it shows

**Visible targets are solved.** Every model found the red ball in both runs (8/8), stopping
0.9–1.0 m away and facing it within a few degrees, typically in 7–10 steps. DeepSeek does it in about a
minute, Qwen and GLM in 2–3 minutes.

**Finding things in another room isn't, for any model.** Kitchen: 0/24 across all rounds. Bed in round 3:
1/8. The logs show the same failures in every model:
- **Angles:** asked to face a doorway "a bit to the left", models swing back and forth (+45°, −45°, +40°,
  −30°…), unable to turn the picture into the right number of degrees.
- **Distances:** believing a 1.5 m drive went "through the doorway" (still 2.5 m away), or that bumping a
  door frame meant reaching the bed.
- **Memory:** after a few turns they lose track of which way they came and re-scan the same walls.

These are spatial-reasoning limits that all four share, so **choosing a different model doesn't fix
them. The driver has to help** (see below).

**Speed and cost differ a lot.** DeepSeek decides in seconds. GLM sometimes thinks over two minutes per
step and was cut off 12 times even with an 8,000-token budget. Qwen costs about 6× DeepSeek per task.

## Recommendation

**Default model: DeepSeek-V4.1-Flash.** It's the brain's default already.
- It matches the others on everything they can actually do, is 2–3× faster, which matters for a
  robot moving in a home, and is among the cheapest (about half a cent per task).
- **The 1017 beta** scored the same and was slightly faster. Switch to it if it leaves beta;
  until then, the stable version is the safer default.
- **Qwen3.8-27B** is the runner-up: fewest bumps, and the only bed success. Worth keeping as an option
  for patient tasks, but not as the default at 6× the cost and 2× the time.
- **GLM-5.3-Flash:** not recommended for driving. Too slow, and too often cut off mid-thought.

**Use it for:** "approach the person/thing you can see", "turn to look at…", "go up to the
fridge" when it's in view, always with a person watching (the take-over is instant).

**Don't rely on it for:** "go to the kitchen" style errands through other rooms, until the driver
improvements below are in.

## Round 4: angle/distance guides + breadcrumb map (2026-10-25)

Same models, tasks and runs as round 3, with three changes to what the AI sees:
- yellow angle marks (+30 … −30) and floor-distance marks (2/3/5 m) on the head camera
- distance lines on the floor camera
- a third image: a top-down map of its own path from odometry, plus its position in words

Results in `~/beam-brain/bakeoff/20261025-185231/`.

| model | ball | kitchen | bed | success | avg time per task | cut-off steps | cost | vs. round 3 |
|---|---|---|---|---|---|---|---|---|
| DeepSeek-V4.1-Flash | ❌ ❌ | ❌ ❌ | ❌ ❌ | 0/6 | 13.3 min | 25 | $0.135 | 2/6 → 0/6 |
| DeepSeek-V4.1-Flash-1017 | ✅ ✅ | ❌ ❌ | ❌ ❌ | 2/6 | 6.7 min | 5 | $0.073 | same |
| Qwen3.8-27B | ❌ ❌ | ❌ ❌ | ❌ ❌ | 0/6 | 13.3 min | 4 | $0.333 | 3/6 → 0/6 |
| GLM-5.3-Flash | ✅ ✅ | ❌ ❌ | ❌ ❌ | 2/6 | 11.5 min | 15 | $0.070 | same |

**The guides made things worse: 4/24 successes, against 9/24 without them.** From the logs:
1. **Aiming too early.** With precise angles available, models turned toward the ball *before* the office
   doorway and clipped its frame. The start position is only 3 cm from the upper door frame, a test
   weakness the earlier "drive straight first" behaviour never hit.
2. **Then stuck in a loop:** "angle right to clear the obstacle" (−20°), then "face the ball" (+20°),
   repeated, never driving in between.
3. **Much more thinking.** Three images and a longer prompt made DeepSeek-Flash, which previously answered
   instantly, think 30–90 s per step and get cut off 25 times. Time and cost per task roughly tripled.
4. Room-to-room navigation **did not improve** (kitchen 0/8, bed 0/8).

**Action taken:** guides are **off by default** again (`AIDriver(guides=False)`; the bake-off has `--guides`
to opt in). The code stays for future experiments.

## Next steps

What the four rounds suggest:
- **Give the models fewer, bigger decisions, not more information.** Their trouble is turning what they
  see into precise motion. The driver should do that, with tools like "go through the doorway on the
  left" or "drive up to the red thing", which the driver carries out with its own geometry (and
  later its own image processing).
- **Fix the start position** (centre it on the doorway line) before the next comparison, and add a
  start position that isn't next to a doorway, so results don't hinge on one tight spot.
- If guides are tried again, test **one at a time** (angle marks only; map only): this round changed
  three things at once, so it can't say which one hurt.

## Round 5: big actions (2026-10-25)

First, **models can draw accurate boxes:** median angle error 0.3–0.5° for DeepSeek and GLM
(`python3 -m beambrain.grounding`). So with big actions the model names what to approach or which doorway
to use, and the driver does the geometry (see `docs/ai-driving.md`). The start position was also moved to
the doorway's centre line, and a new baseline was run with the same setup.

| | Baseline (new start) | Big actions |
|---|---|---|
| Success | **9/24** | 7/24 |
| Ball | 8/8 | 6/8 |
| Kitchen | 0/8 | **1/8**, the first real-run kitchen success (DeepSeek-Flash) |
| Bed | 1/8 | 0/8 |
| Bumps | 77 | 226 |

Results: `~/beam-brain/bakeoff/20261025-210250` (baseline) and `…-215941` (big actions).

**What the logs show:**
- **Used in the right order, big actions are excellent.** GLM's successful ball run took 3 steps and one minute:
  `go_through_doorway` → `approach("the red ball")` → `done`. Before, that took 7–10 steps.
- **`approach` drives in a straight line** to the target. When the target is in the next room, that
  line often crosses the door frame. 14 of 22 approaches ended blocked, and models repeated the same
  failing action instead of using the doorway first.
- **Retries multiply bumps:** each blocked attempt is retried, hence 226 bumps.

**Conclusion:** the split of work is right. The model is good at "which thing, which doorway", and the
driver aims precisely from boxes. But the driver is **blind to obstacles** until it hits one. Fixing that
properly needs either obstacle sensing (ultrasonic/ToF range sensors, or a lidar, as Beam's own
software supported for the BeamPro) or a map with path planning, not more prompting.

**Status:** big actions stay opt-in (`AIDriver(big_actions=True)`, bake-off `--big-actions`). The default AI
driver is unchanged: DeepSeek-V4.1-Flash, no guides, no big actions. Fine for "approach what you can see"
with a person watching.

## Earlier ideas (before round 4)

1. **Angle guides on the camera image:** small tick marks at −30°, −15°, 0, +15°, +30° drawn on the frame
   the AI sees, so "the doorway is at the +15° mark" turns directly into `turn(15)`.
2. **A breadcrumb map:** a small top-down sketch of where the robot has been (from odometry), sent with
   each step, so it stops re-scanning the same walls.
3. **"Go through doorway" as one tool:** the driver centres on the opening and passes through it, which
   removes the step that fails most often.
4. Then **re-run this bake-off**, with `--runs 2`, about 30 cents.
