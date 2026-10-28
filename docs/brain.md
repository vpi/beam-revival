# The brain: face recognition and greetings (experimental)

The **brain** is a service that runs on another computer, such as your Proxmox VM, not on the robot.
It watches the robot's head camera, recognises people you've introduced, and greets them by name
through the robot's speaker. It's built so that more abilities (docking, "what do you see?",
autonomy) can plug into the same connection later.

```
 Robot (lab stick)                         Brain host (Proxmox VM)
 robot screen ── camera frames, 3/s ──▶  beambrain: YuNet face detection + SFace recognition
 speaker      ◀── "Hi Leo!" (Piper voice)   people.json (names + face embeddings, owner-only)
 driving page ◀── face boxes, people list
```

## What it does

- **Face boxes on the driving page.** Unknown faces get a dashed "Who's this?" box. **Click it
  and type a name** to introduce the person. Known faces get a green box with their name.
- **Greetings.** When someone known is seen, the robot says hello (for example "Hi Leo!") and shows
  it on its screen, **at most once per person every 30 minutes** (`--cooldown`).
- **People card** on the driving page: everyone the robot knows, with a thumbnail, a **Forget** button
  (deletes their face data) and **Pause recognition**.
- **Visible to the room:** while a brain is connected, the robot screen shows
  "● face recognition on" (or "paused").

## Privacy

- Face recognition stays on your own machines. Camera frames go robot → brain host over Tailscale.
  Nothing is sent to the cloud. (Talking to Rocky does send words to an AI service: see
  [rocky.md](rocky.md).)
- Only people you name are recognised. Everyone else is just "a face" and isn't stored.
- Stored per person: a name, up to 30 face embeddings (lists of numbers, not photos) and one 64-px
  thumbnail, in `~/beam-brain/data/people.json`, readable only by its owner. **Forget** deletes them.
- Only introduce people who've agreed to it, and use Pause when you have visitors.

## Performance (on the i7-12700K VM)

Face detection takes about 7 ms per frame, a spoken greeting about 60 ms to generate, and frames
arrive 3 times a second. The robot only captures and sends frames; all the work happens on the brain host.

## Setup

**Brain host** (once): on the VM, from the project folder:
```bash
brain-host/install.sh wss://beam-lab.<your-tailnet>.ts.net/ws "$(cat ~/beam-brain/brain.key)"
```
This installs OpenCV, Piper (voice), faster-whisper (Rocky's speech recognition) and the models, writes `/etc/beam-brain.env`, and starts the
`beam-brain` service. To point it at a different robot, or back at the dev simulator,
edit `BEAM_BRAIN_URL` in `/etc/beam-brain.env`, then `sudo systemctl restart beam-brain`.
Logs: `journalctl -u beam-brain -f`.

**Robot:** use the **lab stick** (`beam-usb-lab.img`). Its `beam.conf` already has
`ROBOT_NAME=beam-lab` and the matching `BRAIN_KEY`. The stable stick ignores the brain unless you
add a `BRAIN_KEY` to its `beam.conf`.

The brain connects to only one robot at a time: the lab robot (`wss://beam-lab.<your-tailnet>.ts.net/ws`)
or the dev simulator (`ws://127.0.0.1:8080/ws`). Keep a copy of each setting, for example
`/etc/beam-brain.env.sim`, to switch quickly.

## Try it without the robot

- **Test photo:** open `https://beam-dev.<your-tailnet>.ts.net/robot?test=1&face=1` in one tab (a test
  camera with a face photo) and `https://beam-dev.<your-tailnet>.ts.net/` in another.
- **Your own face:** open `https://beam-dev.<your-tailnet>.ts.net/robot` *without* `?test` on a computer
  with a webcam and allow the camera. That computer then acts as the robot's eyes, ears and speaker.
  Only one robot page can be active, so close the test one first.

## Tests

`python -m pytest tests/test_brain.py` (needs the models in `~/beam-brain/models`). It covers
detection on sample photos, matching the same person versus a different one, the people store,
tracking, the greeting cooldown, and the full loop through the server.
