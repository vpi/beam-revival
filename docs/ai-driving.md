# AI driving and the model bake-off (experimental)

## What's built

- **Virtual house** (`beamctl/world.py`): four rooms (office, living room, kitchen, bedroom), doorways,
  labelled furniture and a red ball. Each room has its own floor (wood, carpet, kitchen tiles), and walls
  and furniture block the robot. The simulator renders two cameras from the robot's real position:
  - **head camera:** eye level, sees far
  - **floor camera:** looks down at the metre or so in front of the robot, like the Beam+'s real nav camera
- **Try it yourself:** `scripts/dev-run.sh` starts the simulator in the house (see the README's
  simulator section). Open `http://<host>:8080/robot?test=1&world=1` (the robot's screen and cameras) and
  `http://<host>:8080/` (the driving page), where `<host>` is the machine running `dev-run.sh`. The
  top-down map with the robot's trail is at `/test/map.png`.
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

The camera geometry lives in `beambrain/geometry.py` (`CameraModel`). By default it matches the simulator;
a real Beam+ is measured once (below), and the brain then uses that.

## Measuring the cameras (real robot)

The real cameras differ a lot from the simulator's: on our Beam+ the head camera is 98° wide with slightly
curved edges, and the floor camera is a fisheye looking down past the mast and phone at roughly the first
metre ahead. They're measured with printed markers, about half an hour with someone placing a sheet:

1. Print `docs/calibration-sheet-letter.pdf` (four 80 mm markers, for the floor camera) and
   `docs/calibration-head-letter.pdf` (one 180 mm marker, for the head camera) at **100 % / actual size**,
   and check the squares with a ruler. (A4 paper: `docs/calibration-sheet.pdf` has the same markers, but
   `beambrain/calibrate.py` assumes the Letter layout.)
2. Measure the floor to the head camera's lens, and the wheel axle to the base's front edge.
3. Put the sheet at measured spots. For each, take a snapshot: `kill -USR1` the brain saves the latest frame
   of both cameras to `~/beam-brain/calibration/` (on the brain only), and add a line to `placements.txt`
   there: `<stamp> <name> <metres ahead of the front edge> <metres left>`, plus `upright` for the head sheet.
   - Floor sheet flat, arrow edge towards the robot: 10, 30, 60 and 90 cm ahead centred, and 30 cm ahead
     40 cm to each side.
   - Head sheet standing upright on the floor, facing the robot: 1.5 m and as far as there's room
     (we used 3.5 m) centred, and 2 m about 30–50 cm to each side.
4. `python3 -m beambrain.calibrate --head-height 1.315 --axle-to-front 0.095` fits both cameras, prints how
   well each placement is predicted from the others, and writes `~/beam-brain/data/cameras.json`. Restart
   the brain; it logs "using the robot's measured cameras". Delete the snapshots afterwards.

Ours: floor camera to about 2 cm over the area it sees (0.3–1.2 m ahead of the axle, about ±0.5 m); head
camera lens over the axle, 1.3 m up and level, seeing the floor from 1.3 m out, to about ±10 cm at 2 m.

**Checking the moves.** `kill -USR2` the brain and it does the one move written in
`~/beam-brain/calibration/move.txt` (`turn 90`, `drive 0.5`, or `dock` / `dock-teach`, below): the AI driver's own code, labelled as the AI
on every screen, stopped by any human input, and logged with what the gyro and wheels measured. On our
Beam+, on carpet: turns of ±90° and 180° landed within 2° (by the gyro, and by eye against tape); drives of
0.5 m stopped 2–3 cm short, as intended, drifting up to 3° in heading.

## Docking (experimental)

The Beam+ backs onto its charger, so the robot needs to see the dock from in front and from behind. Print
`docs/dock-marker-letter.pdf` at 100 % and mount it flat and upright on something stiff behind the dock,
centred on it. Ours has the big marker's bottom edge 36 cm above the floor, 5 cm behind the contact plate;
`beambrain/dock.py` has these numbers, and the distance from the base's front edge to the rear contacts
(40 cm), at the top of its docking part.

1. **Head for the dock** (head camera): the markers' corners, through the measured head camera, give how far the
   sheet is and which way (`python3 -m beambrain.dock` prints it for a snapshot). If the dock is near the picture's
   edge (over 25° off) or the fit is poor, the robot first turns to face it: there the camera model is least sure,
   and once a sighting at 54° was badly out. Then it drives straight towards the dock (the way to it is open) to
   about 1.45 m out. Only the distance and direction are used: the head camera's reading of which way the sheet
   faces has been 10–30° out.
2. **Turn round**, and the brain asks for the phone's rear camera (see `docs/phone.md`; only while docking, and
   the phone says so). The rear camera measures exactly where the robot is (below). More than 15 cm off the dock's
   centre line, it drives forward onto the line and turns round again; if something in front stops that, it backs
   in from where it is, as long as that's within 25 cm of the line.
3. **Back in** slowly (8 cm/s, then 3 cm/s for the last 40 cm onto the contacts). First, if it's more than 6° from
   the angle it wants to back in at, it turns there standing still: a big swing while reversing lost the markers
   from the rear picture. Every rear frame gives the
   camera's full pose against the sheet (OpenCV's `solvePnP` on the markers' corners), so the robot knows how
   turned and how far off the dock's centre line it is, whatever the head camera made of it. It angles back onto
   the line (at most 20°), comes in square for the last 15 cm, and stops when the board reports the charger. It
   gives up if it's still more than 8 cm off the line 25 cm from the contacts (rather than scrape the dock), backs
   8 cm further than it should, or loses the markers early.

What the rear camera sees when docked, and its focal length, are taught once, starting on the charger: the robot
looks, drives straight off half a metre, turns 4° and back (how far the picture moves gives the focal length,
roughly), looks again (the two views must be half a metre apart, which corrects it) and backs on (`dock-teach`,
below). The phone may sit anywhere on the robot, looking any way. It's saved in `~/beam-brain/data/dock.json`.

Start docking with **🔌 Dock on the charger** in the AI driver card on the driving page, with the robot facing the
dock about 1.3–2 m away. Like any AI driving it's labelled on every screen, and any key, the joystick, STOP or the
card's Stop ends it; how it went shows in the card's log. Teaching is done on the brain: `dock-teach` in
`move.txt`, then `kill -USR2` it (`dock` there starts docking too).

## The floor map (experimental)

For "go to the kitchen" the robot needs a map, and it makes its own with the lidar. In the **🗺️ Map** card on the
driving page: **Start mapping**, drive the robot slowly round the rooms it may go in (turn gently: the lidar turns
ten times a second, and fast turns smear its scans), then **Finish mapping**. Starting and ending on the charger marks
the dock on the map, and gives a check that the map closes up.

While mapping, the brain keeps the lidar's turn (1° steps, from the robot's telemetry) with the gyro heading and the
wheels' distance every 5 cm, 3° or 2 s. On Finish it builds the map (`beambrain/lidarmap.py`): each scan's pose
starts from the wheels and gyro and is then nudged (a grid search, ±20 cm and ±6°) until its points best fit the
walls mapped so far; then the scan goes into an occupancy grid (5 cm cells). In a simulated flat, with wheels reading
3 % long and the gyro drifting, the poses stay within about 3 cm and 0.3° of the truth.

The recording and the map stay on the brain (`~/beam-brain/maps/`) and go only to the driving pages: a floor plan of
someone's home is private. `python3 -m beambrain.lidarmap <recording>` rebuilds a recording into a PNG, to try
changes without driving again.

**Where it is on the map** (`beambrain/localize.py`): once there's a map, the brain follows the robot on it, and the
Map card shows it as a red dot pointing its way. Between scans the pose moves by the wheels and gyro; every 3 cm, 2°
or second, the latest lidar turn is matched against the map's walls (the same search as building) and the pose is
corrected. The gyro needs it: on the first real mapping drive it under-read turns by about 4 %, about 100° over ten
minutes (`GYRO_SCALE` takes most of that out). On the charger the robot is on the dock, whatever tracking thought.
If the scans stop fitting (it was carried, or woken away from the dock), it looks for itself everywhere: the scan is
slid over the whole map at every heading (10 cm, 2°) and the best spots refined. That takes about 0.3 s, and it
answers only when one spot clearly beats every other. Replaying the real drive, tracking stayed within about 1 cm,
and finding itself from scratch was right 39 times out of 40 (the other 35 cm out, which tracking then pulls in).

**Driving by the map** (`beambrain/navigate.py`): a route over the map's squares (A*) that keeps the robot's middle
at least 30 cm from anything the map has as a wall, furniture or never seen, and prefers 55 cm, so it goes down the
middle of doorways. The robot follows it at 0.2 m/s, chasing a point 45 cm along the route (turning on the spot first
when that point is well round). Drifting off the route plans again. If something new is in the way, the server's
lidar stop holds the robot; it waits 3 s, then plans round what the lidar sees, and after two tries stops and says
so. Held within 25 cm of a place (a spot by a counter, where the lidar stop keeps it a little short), it finishes
from there instead: turns the saved way and nudges straight onto the spot. It's the AI driver's driving: labelled as the AI, stopped by any human input.

**Docking from anywhere**: when the head camera can't see the dock marker, the Dock button first drives by the map
to the roomiest clear spot in front of the dock (0.9–2.2 m from the sheet, within 40° of straight on; ours, with a
door 1.5 m out, gets one 1.1 m straight out), facing it; then docking carries on as before.

**Named places** (`beambrain/places.py`): drive the robot to a spot, facing the way it should stand there, and save
it in the Map card's 📍 Places with one or more names ("living room, lounge"). The dock is always a place ("dock",
"charger"; renamable), and going there means docking. **Go** next to a place drives there by the map; Rocky can be
asked too (docs/rocky.md). Places are kept with the map on the brain only (`~/beam-brain/maps/places.json`). The map's
origin is the docked pose, so they stay put if the map is made again with the dock in the same spot. A place can also name its room's Home Assistant presence sensors (👤, docs/rocky.md "Who is where").

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
