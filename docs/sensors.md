# Extra sensors

## A USB hub in the base

The Beam+ has a single USB cable from the head down to the base, and it goes to the motor board. To add
sensors, put a **powered** USB hub in the base: that cable into the hub, a short cable from the hub to the
motor board, and the sensors into the hub. We used a StarTech ST4200USBM (4 ports, 7–24 V screw terminals).
Before connecting it, check with a meter that every port gives 5 V and that the upstream plug shows **no**
voltage with the hub powered and nothing else connected (no "backfeed" into the computer).

Power it from the motor board's **J6 "HEAD"** connector, the head's own 12 V feed: the board switches it on
and off with the head, so the hub is on exactly when the computer is. Fuse it (2 A).

Nothing changes in software: the motor board is found by its USB ID wherever it's plugged in. But it now
depends on the hub. Once, at a cold start, the hub failed to come up on USB ("unable to enumerate" in the
kernel log). The motor board, hearing nothing from the computer, power-cycled the robot about 2 minutes
later, and the next start was fine. So the robot recovers by itself, at the cost of a few minutes.

## Ultrasonic distance sensor (A02YYUW)

A waterproof ultrasonic sensor that measures 3 to 450 cm. It connects through a USB serial adapter
(a CP2102 module), either directly to the robot's computer or through a USB hub.

| Sensor wire | CP2102 pin |
|---|---|
| Red (power) | +5V |
| Black (ground) | GND |
| White (sensor TX) | RXD |
| Yellow (mode) | not connected: the sensor then sends a filtered reading about 10 times a second |

The CP2102's TXD and 3V3 stay unconnected. If you use a Grove breakout board in between, go by the wire
colours, not the board's labels: the sensor's plug doesn't follow the Grove pin order.

The robot server (`beamctl/rangefinder.py`) looks for the adapter by its name
(`/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART...`), so it can be plugged in or out at any time, and
the driving page's Robot card shows the distance while it's there. It also feeds the obstacle stop (below).

The adapter is a plain CP2102, not a CP2102N: the RPLIDAR C1's adapter is a CP2102N and is left alone.

A name isn't proof: other things (a robot arm, say) use the same chips. So a port is used only once the
sensor's frames come from it; one that doesn't send them is logged ("doesn't send distance sensor frames") and left
alone for a minute, then tried again. `RANGE_DEVICE` in beam.conf pins the port (see [Which port](#which-port)),
or `none` turns it off.

A reading under 3 cm (inside the sensor's blind zone) counts as something touching the front, not as a clear
way; 0 is the sensor's "no echo". We haven't yet checked on hardware what it reports that close: if it turns
out to report 0, a box pushed against it reads as clear, and only the lidar sees it.

Mount it low at the front, about 5 to 10 cm above the floor and tilted up a few degrees, so it sees low
things the lidar passes over (a box, shoes) and glass. If the reading jumps about with nothing in front, it's
seeing the floor; tilt it up a little more.

**One isn't enough for shoes.** Tested on ours, with a shoe (about 9 cm tall) on the floor at each spot, robot
standing still, the sensor centred at the front:

| | in front of the left wheel | centre | in front of the right wheel |
|---|---|---|---|
| 30 cm | missed | seen (0.37 m) | seen (0.35 m) |
| 60 cm | missed | missed | seen (0.75 m) |
| 1 m | | | seen (1.03 m) |

Its beam is narrower than it looks, and taped to the curved front it pointed a little right; tilted up, it
passed over the shoe further out. So we're adding one on each front corner, facing out over the wheels.

### Several sensors

Any number work, each on its own adapter: with `RANGE_DEVICE` empty the robot uses every one that sends the
frames (one adapter, one sensor; a replugged one comes back as itself). The obstacle stop goes by the nearest
reading, whichever way that sensor faces: its beam is wide, so what it hears may be in front of a wheel; it
crawls while any of them is silent or lost. To show each the way it faces on the driving page, name them in
beam.conf with an angle (degrees, clockwise from straight ahead; see [Which port](#which-port)):

```
RANGE_DEVICE=usb-..._<serial A>-if00-port0@-30,usb-..._<serial B>-if00-port0,usb-..._<serial C>-if00-port0@30
```

Mount the corner ones as level as you can (the curve of the front already points them out), 5 to 8 cm up.
Each needs a USB port: a 4-port hub with the board, the lidar and one sensor on it is full, so use a bigger
powered hub (same 12 V screw terminals) for more.

## Lidar (Slamtec RPLIDAR C1)

A 2D laser scanner: it spins about 10 times a second and measures the distance all around, in one flat
plane, 5 cm to 12 m. It plugs into the hub with its own USB adapter (a CP2102N).

Mount it on top of the base, just in front of the mast, level, with the **arrow on its top pointing to the
robot's front** (its cable then runs back towards the mast). It sees everything taller than its own height;
the ultrasonic sensor covers what's lower.

The robot server (`beamctl/lidar.py`) looks for it by its adapter's name, and uses a port only once it answers
the lidar's device-info request (the log then says "lidar at ... (model ..., firmware ...)"); anything else is
logged ("doesn't answer like a lidar") and left alone for a minute. It spins the lidar while the robot is awake
and stops it in sleep mode. The driving page then has a **Surroundings** card: a top view, front up, with the
lidar's hits, the slice the mast hides, and the ultrasonic sensor's reading as an arc in front. Nothing is
recorded; only the latest turn is kept.

On our Beam+ the lidar's centre is 6 cm behind the base's front edge (3.5 cm ahead of the wheels' axle), and
the mast, 7 cm behind the lidar, hides **135° to 210°** (clockwise from straight ahead),
which the server leaves out. Settings, if yours differs:

| Option | Default | |
|---|---|---|
| `--lidar-device` | `auto` | a serial port path, or `none` to turn it off (`LIDAR_DEVICE` in beam.conf) |
| `--lidar-mask` | `135-210` | degrees to leave out; `''` for none (one it can't read is logged and ignored) |
| `--lidar-rotation` | `0` | degrees to add if the arrow doesn't point to the front |

To find the mask for another mounting: point it as above, look at which directions show hits only a few
centimetres away, and leave those out.

## 8x8 matrix distance sensor (DFRobot SEN0628)

A wide distance sensor: an 8x8 grid of distances covering about 60° across and 60° up and down, up to
about 3.5 m, 15 times a second. Where the ultrasonic gives one distance straight ahead, this one sees the
whole width of the robot's path, and how high things are: a shoe on the floor, a table edge at shin height.

It plugs straight into the hub in the base with a USB-C cable (no adapter: the board has its own RP2040,
which shows up as a USB serial port named "Pico"). It sends all the time, as text, eight lines a frame:

```
y0:1373,1255,4000,4000,4000,4000,402,4000,
...
y7:1583,1429,1286,1214,1115,1057,992,4000,
```

Each number is millimetres; **4000 means no reading** in that cell. Other boards may have the same generic
RP2040 USB id, so a port counts as the sensor only once these frames come from it (as with the others).

**Mounting:** front centre, low (about 10 cm up), level or tilted a little down. Mount it with its **USB-C socket
pointing down**: then row y0 is the top and the first number in a row is the left (checked with a hand over
each half), so no `MATRIX_FLIP` is needed. **No window in front of it**:
plastic or glass bounces its own light back and makes it read short. Leave an open hole.

Each cell is a direction, so with the sensor's height and tilt every reading is a point in front of the robot.
**Only things at least 5 cm tall count** (plus 3 cm per metre away where it goes by geometry, since floor seen at
a slant reads a little short): lower is floor, which the robot drives over. That includes 1–2 cm rubber mats, door
thresholds and flat rugs, and also a sock or a flip-flop. Anything outside the robot's path (`robot_half_width_m`
either side) or higher than 1.6 m is left out too. Each cell is judged on the middle of its last 3 readings (blank
in 2 of them: nothing): floor seen at a slant is noisy, and over a black kitchen mat single frames read up to 7 cm
high, two in a row up to 5, while the middle of three stayed under 2.5 cm. The nearest of the rest is the gap ahead, which the
[obstacle stop](#obstacle-stop) uses like one distance sensor. The driving page shows it as **8x8 sensor**.

| beam.conf | Server option | Default | |
|---|---|---|---|
| `MATRIX_DEVICE` | `--matrix-device` | auto | its port, or `none` |
| `MATRIX_HEIGHT_CM` | `--matrix-height` (m) | 10 cm | lens above the floor |
| `MATRIX_TILT_DEG` | `--matrix-tilt` | 0 | degrees it points down (negative: up) |
| `MATRIX_FLIP` | `--matrix-flip` | none | `x` if left and right come out swapped, `y` if upside down |
| | `--matrix-to-front` | 0 | how far behind the front edge it sits, metres |

Get the height and tilt right: too little tilt set, and the floor ahead looks like an obstacle (the robot stops
for nothing); too much, and real obstacles near the floor are taken for floor. The tilt is easiest to get from
the floor itself: with nothing in front, the bottom two rows read the floor, at about height ÷ sin(row's angle
down), where row 7 looks 26.25° below the sensor's axis and row 6 18.75°, plus the tilt. Ours, 25 cm up, read
0.43 m and 0.57 m. But those come out short: each cell covers a 7.5° cone, and on a slanted floor it reports the
near edge of it, so the floor alone suggests too much tilt (ours: 8°, which hid a shoe completely). Measure the
height, then check with a shoe at 1 m: we ended at 24.5 cm and 3.5° (at 2° the floor counted, at 5° the shoe at
1 m was lost).

**Learn the floor.** Even set right, geometry misjudges the bottom rows: a corner cell that reads a little short
looks like a shoe, and a shoe's low toe like floor. So with the robot still and the floor clear for 1 m in front, press
**Learn the floor (8x8 sensor)** on the driving page: for 3 seconds it records what each cell reads as floor
(kept in the server's state folder, `matrix-floor.json`). From then on a cell that saw floor tells height by how much
shorter than its own floor it reads (a shoe's toe at 0.4 m: 6.5 cm, counted; a flip-flop: 3 cm, not); cells that
never saw floor go by geometry as before. A cell that gives no reading at all counts as nothing: black things send
nothing back, and a black rubber mat looked exactly like that (a black shoe is still seen by its collar). It refuses if something is in the way, if the robot moves, or while it's asleep, and it's set aside if
the height, tilt or flip change (learn it again). Learnt on carpet, it worked on the kitchen's vinyl too; learn it again on a different kind of floor if the robot
slows for nothing there.

**What goes out of view is gone.** The view meets the floor about 0.4 m out, and covers 30° either side, so a low
thing drops under it as the robot comes up, and things beside the path leave it to the side. For a while it
remembered low things that went out of view and brought them closer as it drove; in narrow passages that held
the robot still for what it had passed (a door frame's foot, someone's feet) and the AI gave up, so it was
taken out (2026-11-11). Only what's in view counts. So a low thing (5–15 cm) that goes under the view at about
0.35 m may be nudged at the slow approach speed; anything taller (a shoe's collar) stays in view and stops it.

## Obstacle stop

With either sensor connected, the robot server limits **forward** driving by what's in front: full speed
beyond 0.6 m, slowing down closer, and no forward motion from 0.15 m (gaps in front of the robot's front
edge); below 0.03 m/s it stops rather than creeping the last centimetres. The lidar counts only what's in the
robot's path (half the base plus 2.5 cm either side of its centre line: 18 cm on a Beam+, whose base is 31 cm
wide; `LIDAR_SIDE_CM` if the lidar sits off that line), not things beside it; the ultrasonic sensor sees
what's lower than the lidar. Backing away and turning are never limited, so you can always get out. If a
connected sensor goes silent, forward is held to a crawl (0.1 m/s). It applies to every driver, the AI too.
The same crawl holds if a sensor disappears after it was seen (unplugged, a USB reset, the hub dropping out),
until it's back; the status then says `"lost": true`. A sensor named with `--range-device PATH` or
`--lidar-device PATH` is expected from the start, so without it the robot crawls from boot.

Each hold that lasts a second is logged on the robot with what held it ("forward held by sensor 0.19 m ahead;
8x8 0.19; ultrasonic 0.87"), and when it's free again. The AI's navigator logs it too, and when
a distance sensor (not the lidar) holds it, it plans the next route round that spot as well: the lidar can't see it.

The driving page's Drive card shows it ("clear", "slowing", "blocked") and has a switch to turn it off. Off
is for that driver only, with a warning banner while it lasts, and it's on again when they leave the page.
`--no-obstacle-stop` turns it off for good. In beam.conf, `ROBOT_WIDTH_CM` (the base, measured; default 31),
`LIDAR_SIDE_CM` (where the lidar sits off the centre line, + to the right; ours -3), `OBSTACLE_STOP_CM` (15) and
`OBSTACLE_SLOW_CM` (60). Until 2026-11-10 they were a guessed 50 cm width and 25 / 80 cm, too cautious for a
home: in a narrow corner a wall beside the robot counted as an obstacle 0 m ahead, and the AI's planner (which
assumed the same 50 cm) planned routes the obstacle stop then refused. To find `LIDAR_SIDE_CM`, park beside a
wall, measure from it to the wheel, and compare with what the lidar says. At full speed the robot needs about 3 cm to stop.

Tried on our Beam+ on the floor (with the old 80 / 25 cm): driving at 0.08 m/s towards a box, it began slowing
at 0.8 m and came to a stop about 0.26 m before the box, then backed away normally. On blocks the wheels whirr even when it's told
to stand still (the board steering by its gyro, see docs/motor-protocol.md), so judge it on the floor.

## Blocked wheels

Some things no sensor here sees: a shoe beside the ultrasonic sensor's narrow beam, a low step. So the robot
server also watches the wheels (`beamctl/stall.py`). If the robot is told to drive at 0.08 m/s or more (or to
turn on the spot), and for a whole second its wheel encoders show it moving at less than a quarter of that, it
stops trying in that direction. The driving page then shows "Wheels blocked"; backing up or turning away is
free, and letting go clears it. It holds for every driver, even with the obstacle stop turned off. On a trip
to a place, the AI stops and says it got stuck, rather than pushing again.

It can't see a light thing pushed along (a shoe sliding on tiles): the wheels still turn then. Slower than
0.08 m/s isn't checked, since the board may not move at all at a crawl on a thick rug; docking's last
centimetres onto the contacts are slower than that.

The status's `drive.wheels` shows the speeds measured from the encoders (`measured_linear`, m/s;
`measured_angular`, rad/s), whether a way is held (`blocked`), and how many times it has held since start.

## Which port

With only the sensors plugged in, `auto` is fine. If something else on the robot uses a Silicon Labs USB
serial chip (a robot arm, another sensor), pin each sensor to its own adapter in beam.conf, so the robot never
even opens the other one (opening a port can reset what's behind it). On the robot, `ls /dev/serial/by-id/`
lists the adapters; the names include each adapter's serial number, so they stay the same whichever USB socket
it's in:

```
LIDAR_DEVICE=usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_<serial>-if00-port0
RANGE_DEVICE=usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_<serial>-if00-port0
```

Several distance sensors go on one line, separated by commas, each optionally with `@degrees` for the way it
faces ([Several sensors](#several-sensors)).

A pinned port is still checked: if what answers isn't the sensor, it isn't used, and the obstacle stop treats
the sensor as lost (driving forward is slowed to a crawl) until it's fixed. A value the robot doesn't
understand means `auto`, never `none`, and the robot screen says so.
