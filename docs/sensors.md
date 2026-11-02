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

The robot server (`beamctl/rangefinder.py`) finds the adapter by its name
(`/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART...`), so it can be plugged in or out at any time, and
the driving page's Robot card shows the distance while it's there. `--range-device PATH` names another port;
`--range-device none` turns it off. It also feeds the obstacle stop (below).

The adapter is a plain CP2102, not a CP2102N: the RPLIDAR C1's adapter is a CP2102N and is left alone.

Mount it low at the front, about 5 to 10 cm above the floor and tilted up a few degrees, so it sees low
things the lidar passes over (a box, shoes) and glass. Its beam is wide: if the reading jumps about with
nothing in front, it's seeing the floor; tilt it up a little more.

## Lidar (Slamtec RPLIDAR C1)

A 2D laser scanner: it spins about 10 times a second and measures the distance all around, in one flat
plane, 5 cm to 12 m. It plugs into the hub with its own USB adapter (a CP2102N).

Mount it on top of the base, just in front of the mast, level, with the **arrow on its top pointing to the
robot's front** (its cable then runs back towards the mast). It sees everything taller than its own height;
the ultrasonic sensor covers what's lower.

The robot server (`beamctl/lidar.py`) finds it by its adapter's name, spins it while the robot is awake and
stops it in sleep mode. The driving page then has a **Surroundings** card: a top view, front up, with the
lidar's hits, the slice the mast hides, and the ultrasonic sensor's reading as an arc in front. Nothing is
recorded; only the latest turn is kept.

On our Beam+ the mast is 7 cm behind the lidar and hides **135° to 210°** (clockwise from straight ahead),
which the server leaves out. Settings, if yours differs:

| Option | Default | |
|---|---|---|
| `--lidar-device` | `auto` | a serial port path, or `none` to turn it off |
| `--lidar-mask` | `135-210` | degrees to leave out; `''` for none |
| `--lidar-rotation` | `0` | degrees to add if the arrow doesn't point to the front |

To find the mask for another mounting: point it as above, look at which directions show hits only a few
centimetres away, and leave those out.

## Obstacle stop

With either sensor connected, the robot server limits **forward** driving by what's in front: full speed
beyond 0.8 m, slowing down closer, and no forward motion from 0.25 m (gaps in front of the robot's front
edge). The lidar counts only what's in the robot's path, not things beside it; the ultrasonic sensor sees
what's lower than the lidar. Backing away and turning are never limited, so you can always get out. If a
connected sensor goes silent, forward is held to a crawl (0.1 m/s). It applies to every driver, the AI too.

The driving page's Drive card shows it ("clear", "slowing", "blocked") and has a switch to turn it off. Off
is for that driver only, with a warning banner while it lasts, and it's on again when they leave the page.
`--no-obstacle-stop` turns it off for good; `--obstacle-stop-m` and `--obstacle-slow-m` set the distances.

Tried on our Beam+ on the floor: driving at 0.08 m/s towards a box, it began slowing at 0.8 m and came to a
stop about 0.26 m before the box, then backed away normally. On blocks the wheels whirr even when it's told
to stand still (the board steering by its gyro, see docs/motor-protocol.md), so judge it on the floor.
