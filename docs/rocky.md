# Rocky: a personality for the robot (experimental)

Rocky is the Eridian engineer from *Project Hail Mary*, living in the Beam+. He has an animated face on the
robot screen and answers out loud, in short in-character sentences. The idea and much of the code come from
[desk-robot](https://github.com/cgro00/desk-robot) (MIT), a small desk robot with the same character.

He's built one or two features at a time, each tried on the lab stick before the next:

| Step | What | State |
|---|---|---|
| 1 | Face, personality and voice (Piper, or the Fish Audio Rocky clone); you **type** to him from the driving page | **done** |
| 2 | He **listens** through the robot's microphone ("hey Rocky"), only while awake and nobody drives, with a sign on screen | **done** |
| 3 | He **sees**: "what do you see?", "what's behind you?" (head, floor or rear camera), greetings by name | planned |
| 4 | Polish: a wave wakes him, a better voice, a personality editor | planned |

## Step 1: typed chat

Needs the brain ([brain.md](brain.md)) with an AI key (`~/beam-brain/ai.key`). When the brain is connected,
the driving page has a **🤖 Talk to Rocky (AI)** card. Type a message and press Enter:

1. The robot screen's eyes turn purple and show **thinking** while the AI answers (about 3 seconds).
2. Rocky's reply is spoken in the room (the Piper voice) and shown in a purple bubble labelled
   **"🤖 Rocky (AI)"**, while his face shows the reply's emotion and his mouth moves with his voice.
3. Every driver's Rocky card shows the exchange.

He knows who typed (the driver's Tailscale name) and the names of people the face recognition sees, and
remembers the last 8 exchanges (forgotten after 10 quiet minutes). He gets no camera pictures yet, so he
won't describe the room.

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
   spoke and how long it took to make.

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
- **Who's talking:** if the face recognition sees exactly one person it knows, he takes it that they spoke.
- **On the brain host, nothing to the cloud:** Silero VAD finds speech, Smart Turn decides when you've
  finished a sentence (rather than paused), and Whisper (`base.en`) turns it into text, all locally. The
  sound is never stored. Only what was said **to him** goes to the AI service, as with typing; anything
  else is dropped and logged only as a word count.
- **His own voice:** the robot page pauses the microphone while he speaks, so he doesn't answer himself.
- The pipeline is ported from desk-robot. Models download once into `~/beam-brain/models`, pinned and
  checked; `--no-ears` on the brain turns listening off entirely.

## How it works

```
 driving page ──"chat"──▶ robot server ──(adds the driver's name)──▶ brain (beambrain/rocky.py)
                                                                       │ AI service: reply + emotion tag
 robot screen ◀── "face": thinking, then "say": text + emotion + voice ◀┘
 driving pages ◀── "rocky": the exchange
```

Every reply starts with an emotion tag (`[happy] Yes yes yes. It work!`) that sets the face; the brain
removes it, any hidden thinking and markdown, and cuts the reply to 3 sentences before it's spoken.

To change him: the personality is `SYSTEM_PROMPT` in `beambrain/rocky.py`; the faces are the `PACK` in
`beamctl/web/rocky-face.js`. The AI model is the brain's `--ai-model` (DeepSeek V4.1 Flash by default).
