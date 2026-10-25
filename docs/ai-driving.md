# AI driving and the model bake-off (experimental)

## What's built

- **Virtual house** (`beamctl/world.py`): four rooms (office, living room, kitchen, bedroom), doorways,
  labelled furniture and a red ball. Each room has its own floor (wood, carpet, kitchen tiles), and walls
  and furniture block the robot. The simulator renders two cameras from the robot's real position:
  - **head camera:** eye level, sees far
  - **floor camera:** looks down at the metre or so in front of the robot, like the Beam+'s real nav camera
- **Try it yourself:** `scripts/dev-run.sh` starts the simulator in the house. Open
  `https://beam-dev.<your-tailnet>.ts.net/robot?test=1&world=1` (the robot's screen and cameras) and
  `https://beam-dev.<your-tailnet>.ts.net/` (the driving page). The top-down map with the robot's trail is at
  `/test/map.png`.
- **AI driver** (`beambrain/driver.py`): each step, the model sees both cameras plus a short history of
  its own moves, and calls one tool: `turn(degrees)`, `drive(meters)`, `say(text)`, `done` or `give_up`.
  - Moves run closed-loop on the gyro and wheel odometry, and stop early if the robot is blocked.
  - It's started from the **🤖 AI driver** card on the driving page.
- **Human first:** any key, joystick move, STOP or the **Take over** button stops the AI immediately and for good.
- **Clearly labelled:** while the AI is in charge, a purple **"🤖 AI is driving · …"** banner shows on the robot
  screen and on every driving page. Anything the AI says appears as "🤖 AI: …" in purple.

## Big actions (experimental, `AIDriver(big_actions=True)`)

Asking models for angles and distances doesn't work: they swing ±20° around a target. But they're
very good at **drawing a box around something** (median error under 0.5°, measured with
`python3 -m beambrain.grounding`). So with big actions the model only names the target, and the
driver does the geometry:

- **`approach("the red ball")`:** asks the model for the target's box in the head camera, then turns by
  the box's angle and drives by the floor distance under the box's bottom edge (from the camera's
  height and tilt). It looks again after each move. In the last metre, where the target's base drops
  below the head camera's view, it switches to the floor camera. It stops about 1 m away, facing the target.
- **`go_through_doorway("the doorway on the left")`:** box the doorway with the head camera, turn, drive
  to about 0.9 m before it, line up on the floor camera's view of the gap between the walls, then drive through.
- Both report back in words ("arrived: about 1.0 m from the red ball"; "stopped short: probably a wall
  in between; go through the doorway first"), so the model can plan its next step.

The camera geometry lives in `beambrain/geometry.py` (`CameraModel`). It matches the simulator; the real
Beam+ cameras need measuring once: field of view, head camera height and tilt, and the patch of floor the
floor camera sees.

## The bake-off

`python3 -m beambrain.bakeoff` runs the same tasks for each model through the real code path:
simulated motor board → motor server → camera feed → brain → model on Synthetic. Each trial is scored
automatically from the simulator's ground truth, and a map of the path is saved for each one.

| Task | Prompt (shortened) | Pass if, when the model says "done"… |
|---|---|---|
| ball | find the red ball, stop ~1 m away facing it | ≤ 1.3 m from it and facing it (±30°) |
| kitchen | go to the kitchen | the robot is inside the kitchen |
| bed | find the bed, stop ~1 m away facing it | ≤ 1.3 m from it and facing it (±40°) |

The robot starts in the office facing the living-room doorway. Each trial allows at most 20 steps.
See `docs/ai-driving-results.md` for results.
