# Rocky: a personality for the robot (experimental)

Rocky is the Eridian engineer from *Project Hail Mary*, living in the Beam+. He has an animated face on the
robot screen and answers out loud, in short in-character sentences. The idea and much of the code come from
[desk-robot](https://github.com/cgro00/desk-robot) (MIT), a small desk robot with the same character.

He's built one or two features at a time, each tried on the lab stick before the next:

| Step | What | State |
|---|---|---|
| 1 | Face, personality and voice (Piper, or the Fish Audio Rocky clone); you **type** to him from the driving page | **done** |
| 2 | He **listens** through the robot's microphone ("hey Rocky"), only while awake and nobody drives, with a sign on screen | **done** |
| + | He switches **Home Assistant** lights and devices when asked ("turn on the stove lamp") | **done** |
| 3 | He **sees** when asked: "what do you see?", "is the floor clear?" (head and floor cameras) | **done** |
| 3b | "What's behind you?" (the phone's rear camera), and looking without being asked | later |
| 4 | Polish: a **wave** wakes him, a **faster voice** (streamed; other Fish models and settings compared, the current one kept), a **personality editor** | **done** |

## Step 1: typed chat

Needs the brain ([brain.md](brain.md)) with an AI key (`~/beam-brain/ai.key`). When the brain is connected,
the driving page has a **🤖 Talk to Rocky (AI)** card. Type a message and press Enter:

1. The robot screen's eyes turn purple and show **thinking** while the AI answers (about 3 seconds).
2. Rocky's reply is spoken in the room (the Piper voice) and shown in a purple bubble labelled
   **"🤖 Rocky (AI)"**, while his face shows the reply's emotion and his mouth moves with his voice.
3. Every driver's Rocky card shows the exchange.

He knows who typed (the driver's Tailscale name) and the names of people the face recognition sees, and
remembers the last 8 exchanges (forgotten after 10 quiet minutes). He looks through his cameras only
when a question needs it (step 3).

**The face** is desk-robot's: two eyes on a 128×64 grid with twelve emotions (neutral, happy, sad, angry,
surprised, sleepy, thinking, curious, skeptical, excited, love, confused), blinks, glances and small
flourishes like tears, hearts or a "?". It replaces the robot screen's old round eyes, in the same colours:
white when idle, green with a driver connected, and **purple whenever the AI speaks or drives**, so the room
can always tell AI from a person. It's on the robot screen only, not on the phone panel.

**Privacy:** what you type, the recent conversation, your first name and the names of people in view go to
the AI service (Synthetic). Nothing is stored on disk. The Piper voice is made on the brain host.

### Voice: Piper or Fish Audio

Piper (local, free) is the default. Optionally, Rocky can speak with a **Fish Audio** voice: by default the
community-made Rocky clone that desk-robot uses (a fan upload for personal use; it may disappear one day).

1. Create an API key at [fish.audio](https://fish.audio) and add **API credit** on its developers page (API
   credit is separate from app credit). Prices per desk-robot's README, October 2026: about $2.25 per
   1,000 replies with Fish's standard model.
2. Put the key in `~/beam-brain/fish.key` on the brain host (`chmod 600`) and restart `beam-brain`.
3. The Rocky card gets a **Voice** switch (shared by every driver, and remembered across restarts in `~/beam-brain/data/rocky.json`). Greetings use the same voice. Each reply in the card shows which voice
   spoke and how long until its sound started. The Fish voice is **streamed**: the robot plays it as Fish
   makes it, so he starts talking about 0.3 s after his reply is ready (instead of ~1.3 s for the whole clip).

If Fish fails (no credit, no internet), Rocky speaks with Piper instead and the driving page says why. With
the Fish voice, Rocky's reply text also goes to Fish Audio. Other voices: `--fish-voice <id>` (the id from a
voice's page on fish.audio) and `--fish-model` in `BEAM_BRAIN_ARGS`.

**Asleep:** no Rocky. The driving page says so, and the robot's screen, cameras and microphone stay off.

## Step 2: talking to him in the room

When **nobody is driving**, say **"hey Rocky"** and talk normally; he answers out loud. For a minute after
each exchange you don't need his name. "Hey Rocky" alone gets a "Question?" (after a short wait, in case
you go on).

- **When he listens:** only while the robot is awake, the brain is connected, and **no driver is connected**
  (a driver's conversation is theirs; drivers type to him instead). The server enforces it: the moment a
  driver connects, the microphone stops going to the brain. Asleep, nothing is captured at all.
- **Shown in the room:** "👂 Rocky is listening · say "hey Rocky"" in the robot screen's corner whenever
  the microphone goes to the brain, and "👂 Rocky is listening to you" (highlighted) after his name.
- **Switch it off:** the robot's phone has a **👂 Rocky is listening · tap to stop** button. Off, the robot
  screen shows "🔇 Rocky's microphone is off" and nothing goes to the brain until someone taps it on again;
  the switch is remembered across restarts. (Typed chat from drivers still works.)
- **Pauses mid-sentence:** if you go on talking while he's still thinking (his face shows it), he drops his
  half-made answer and answers your whole sentence. Once he has started speaking it's too late; say it again.
- **Who's talking:** if the face recognition sees exactly one person it knows, he takes it that they spoke.
- **On the brain host, nothing to the cloud:** Silero VAD finds speech, Smart Turn decides when you've
  finished a sentence (rather than paused), and Whisper (`base.en`) turns it into text, all locally. The
  sound is never stored. Only what was said **to him** goes to the AI service, as with typing; anything
  else is dropped and logged only as a word count.
- **His own voice:** the robot page pauses the microphone while he speaks, so he doesn't answer himself.
- **Or wave:** facing him, wave a raised hand beside your head for a second or two (keep your face in view;
  walking up to him, or past him, doesn't count). He wakes as if you'd said
  "hey Rocky" ("👋 Rocky saw your wave" on screen, then "Question?"), and you just talk. This is worked out
  on the brain from the head camera's frames he already gets for faces; no picture goes anywhere. It only
  works when he'd listen anyway (awake, nobody driving, microphone on). `--no-wave` turns it off.
- The pipeline is ported from desk-robot. Models download once into `~/beam-brain/models`, pinned and
  checked; `--no-ears` on the brain turns listening off entirely.

## Step 3: seeing

Ask him anything he'd need to see ("what do you see?", "what am I holding?", "is the floor clear?") and he
takes **one picture** and answers from it. He chooses the camera: the **head** camera (forward, at head
height) or the **floor** camera (the floor just in front of his wheels).

- **Only when asked:** he gets a "look" tool, not a video feed. Questions that don't need eyes send no
  picture, and he takes at most 2 pictures per answer.
- **Shown in the room:** the robot screen shows **"📷 Rocky is looking"** (or "looking at the floor") for a
  few seconds each time a picture goes to the AI. Each driver's Rocky card shows it too
  ("📷 Rocky (AI) looked with the head camera").
- **Honest:** he describes only what's in the picture, says so when it's dark or unclear, and knows names
  only from the face recognition.
- **Asleep:** the cameras are off, so there's nothing to look with.
- **Speed:** about 4 s for an answer that needs a picture. On test pictures, DeepSeek V4.1 Flash was the
  quickest of the four models and as accurate as the others.
- **Privacy:** the picture (640 px wide from the head camera, 320 px from the floor camera) goes to the AI
  service with the question. It's not stored. `--no-eyes` on the brain turns sight off.

## Personality editor

The Rocky card on the driving page has a **Personality** section (click to open): the text that tells the AI
who he is and how he talks, which you can rewrite and **Save**. **Undo edits** goes back to what's saved,
**Default** to the original Rocky.

- **What you can change:** his character, his way of talking, his favourite words and the example replies (up
  to 4,000 characters). **What stays fixed:** where he lives (the robot), replies short enough to speak (at
  most 3 sentences, no lists or emoji), the emotion tag that sets his face (examples should start with one
  too, e.g. `[happy]`), staying honest about what he sees, and the home rules. He still answers to
  "hey Rocky", and his replies are still labelled "🤖 Rocky (AI)". The greetings he says to people he
  recognises aren't part of it (`GREETINGS` in `beambrain/rocky.py`).
- **For everyone:** a change applies to every driver and the room at once, and every driver sees "*name*
  changed Rocky's personality". The card shows who changed it last and when. He forgets the conversation so
  far, so his old replies don't pull him back to the old style.
- **Who may:** with a list of household drivers (`--home-drivers`, see Home Assistant below), only they may;
  without one, any driver may, like the voice switch.
- **Kept** across restarts in `~/beam-brain/data/rocky.json` (only when it differs from the default).
- **Privacy:** like the rest of his prompt, the text goes to the AI service with every message.

## Home Assistant

Ask him to switch, set or check something in the home ("hey Rocky, turn on the stove lamp", "is the kitchen
light on?") and he does it through Home Assistant, then confirms in his own words. Everything else is still
just conversation.

- **Who may:** anyone **in the room** (by voice), and only the **drivers you list**: typed chats from anyone
  else get no home tools, and he says he can't do it for them. Drivers are matched by their Tailscale
  **login** (which the robot server adds, not the page), since display names can be changed.
- **What he can reach:** only what you expose to Assist in HA (Settings → Voice assistants → Expose), as
  the HA user that owns his token. HA's tools are its standard Assist ones (turn on/off, lights, fans, media,
  states), so leave locks, alarms and garage doors unexposed.
- **Labelled as AI:** his HA user's name appears in HA's logbook, and each driver's Rocky card shows what
  he did, e.g. "🏠 Rocky (AI) → HassTurnOn Kitchen Stove Lamp ✓".
- **Speed:** about 3–5 s for a command. Use the device's exact HA name or give it an **alias** in HA
  ("Stove Lamp"); otherwise he looks the name up first, which takes a few seconds longer.
- **Privacy:** the AI service sees HA's list of tools and what the tools answer (device names and states)
  for the requests that use them.

Setup:

1. In HA, add the **Model Context Protocol Server** integration.
2. Create a non-admin HA user for him (e.g. "Rocky"), log in as it and create a **long-lived access token**
   on its profile page.
3. On the brain host, put the token in `~/beam-brain/ha.key` (`chmod 600`).
4. Add to `BEAM_BRAIN_ARGS` in `/etc/beam-brain.env`, then restart `beam-brain`:
   `--home-url http://<home-assistant>:8123/api/mcp --home-drivers <your Tailscale login>[,<another>]`

## How it works

```
 driving page ──"chat"──▶ robot server ──(adds the driver's name)──▶ brain (beambrain/rocky.py)
                                                                       │ AI service: reply + emotion tag
 robot screen ◀── "face": thinking, then "say": text + emotion + voice ◀┘
 driving pages ◀── "rocky": the exchange
```

Every reply starts with an emotion tag (`[happy] Yes yes yes. It work!`) that sets the face; the brain
removes it, any hidden thinking and markdown, and cuts the reply to 3 sentences before it's spoken.

To change him: the personality editor on the driving page (above), or its default, `PERSONALITY` in
`beambrain/rocky.py`, where the fixed rest of his prompt is too; the faces are the `PACK` in
`beamctl/web/rocky-face.js`. The AI model is the brain's `--ai-model` (DeepSeek V4.1 Flash by default).
