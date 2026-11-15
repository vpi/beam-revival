# The robot arm (optional, experimental)

A Waveshare RoArm-M2-S on the stand: three joints (base, shoulder, elbow) and a gripper, driven by its own
ESP32 over USB serial. Nothing about it runs unless `ARM_DEVICE` is set in `beam.conf`.

## Fitting it

- **Power:** 12 V from the robot (it takes 7–12.6 V). Its ESP32 runs on USB power, so the arm's 12 V switch
  only cuts the servos.
- **USB:** the arm's board has two USB-C ports. One is a separate adapter for a lidar the board can carry, which
  stays silent; the ESP32 is on the other one. Plug that one into the robot's head hub.
- **`beam.conf`:** `ARM_DEVICE=` the arm's name from `/dev/serial/by-id/` (`ls -l /dev/serial/by-id/` on the
  robot: a `CP2102N` entry that isn't the lidar). Set `LIDAR_DEVICE` too: both use the same kind of USB chip,
  and the arm is never looked for automatically.
- **Its stow:** the arm moves by itself when it starts (its firmware's start pose, forearm level), then plays
  its own "boot mission". Ours ends folded along the stand's front: elbow 180°, base 93°, shoulder −90°, the
  pose the robot calls **stowed**. Set it up once (the robot never changes it), each a line sent to the arm's
  serial port at 115200 with nothing else talking to it (stop the motor server first):
  ```
  {"T":222,"name":"boot","step":"{\"T\":301,\"mode\":0}"}
  {"T":222,"name":"boot","step":"{\"T\":300,\"mode\":0,\"mac\":\"00:00:00:00:00:00\"}"}
  {"T":222,"name":"boot","step":"{\"T\":121,\"joint\":3,\"angle\":180,\"spd\":30,\"acc\":10}"}
  {"T":224,"name":"boot","delay":4000}
  {"T":222,"name":"boot","step":"{\"T\":121,\"joint\":1,\"angle\":94,\"spd\":20,\"acc\":10}"}
  {"T":224,"name":"boot","delay":5500}
  {"T":222,"name":"boot","step":"{\"T\":121,\"joint\":2,\"angle\":-90,\"spd\":20,\"acc\":10}"}
  ```
  The first two turn its radio control (ESP-NOW) off at every start. The angles are for our mount: find yours by
  moving the arm with the Arm card, and check the whole mission with `{"T":602}`. Any byte sent to the arm while
  the mission plays cancels the rest of it, so leave it alone for a minute after it starts.

## What the robot does with it

- **Driving needs the arm stowed.** With the arm out, the wheels don't turn, for anyone, the AI included. While
  the robot can't tell where the arm is (it's starting up, doesn't answer, or is unplugged), it drives only slowly.
- **The arm doesn't leave its stow while the wheels turn.**
- **STOP** (the page's, or the E-stop) freezes the arm where it is, and it takes no orders until STOP is released.
- **Sleep:** the arm stows, then its servos go limp. Waking turns them on again where the arm is: it doesn't move.
- **Who moves it:** whoever is driving. Another page is refused while someone drives. A move stops if the page
  that started it closes.
- **The AI (Rocky)** may only play a named gesture (`wave`, more to come), from the stow and back to it, when someone
  asks him; never while a person is at the controls or for 10 s after they last touched one, and any person's input
  (driving, an arm button, STOP) stops his move at once. His moves show purple on the robot screen and phone.
- **Moves** are one joint at a time, 20° a second by default (`--arm-speed`, at most 45), within limits tighter than
  the firmware's. A move freezes if a joint's load jumps (it met something), it stops getting closer, or it takes
  too long.
- **While the arm starts** nothing is sent to it, for up to a minute, until it says its boot mission is done: any
  byte that reaches it then would cancel the rest of the mission (its radio-off steps and the stow).
- **Never sent:** `{"T":0}`, which the firmware calls an emergency stop but which drops the arm and leaves it deaf
  for 10 seconds.

## On the driving page

The **🦾 Arm** card (shown only with an arm) says what the arm is doing, each joint's angle, and the last thing it
refused or why it froze. **Hold** − or + to move a joint; let go and it stops (the page sends a stop at once, and
the server freezes the arm half a second after the last message anyway). **Stow** closes the gripper (if it was left open), then lowers the shoulder, folds the elbow and turns the base: along the stand,
**Stop arm** freezes it, **Let go** switches its servos off (it asks first: the arm may drop), **Hold again**
switches them back on where it is. With the arm out, the Drive card says so. **💡 Light** switches the gripper's LED
on at the slider's brightness, or off; it goes off by itself when the robot sleeps or the page that switched it on
closes. The arm camera's view is at the top of the card (click to enlarge).

**Gestures:** a button for each (from the stow, back to it). **Teach one:** stow the arm, press ● Record a gesture,
move it with − and + (each release is a move; the same joint again straight after updates that move), then
💾 Save as… and give it a name ("high five"). It plays your moves back in the same order, from the stow, then
stows; Rocky can do it too when asked. ✕ Cancel recording drops the moves instead. ✕ next to a
taught gesture forgets it. Taught gestures are kept on the robot
(`arm-gestures.json` in the server's state folder). Wave and fist bump are built in.

The robot screen shows a green "A driver is moving the arm" (purple "AI is moving the arm" for Rocky) while
it moves, and the phone panel says the same.

For other clients the server takes `arm_jog` `{joint, dir: -1|1}` (repeat while held), `arm_joint`
`{joint: base|shoulder|elbow|gripper, deg}`, `arm_stow`, `arm_stop`, `arm_let_go`, `arm_hold`. Its state is in the
status as `arm`: `state` (off, booting, lost, stowed, moving, holding, limp), `by`, `joints`, `loads`, `torque`,
`volts`, `why` (the last refusal or freeze), `drive` (null, `out` or `unknown`) and `light` (0..255). `arm_light` `{level: 0..255}` sets the light.

## Its radio

Out of the box the arm obeys ESP-NOW commands from any ESP32 nearby and runs a Wi-Fi hotspot. Its boot mission
turns ESP-NOW off, and the robot turns it off again whenever it connects. The stock firmware can't turn the
hotspot off: give it a long random password (`T:407` / `T:402`) and keep that on the robot only.
