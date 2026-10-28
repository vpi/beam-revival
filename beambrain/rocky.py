"""Rocky: the robot's personality. A typed or spoken message in, a short in-character spoken reply out.

The character, the emotion tag at the start of every reply, and the rules that keep replies short and honest are
adapted from desk-robot by Casey (https://github.com/cgro00/desk-robot, MIT License, Copyright (c) 2026 Casey),
rewritten for a Beam+ telepresence robot. Rocky is the Eridian engineer from Andy Weir's *Project Hail Mary*;
this is fan work.

Steps 1-2 (see docs/rocky.md): typed chat from drivers, and speech in the room ("hey Rocky", beambrain/ears.py).
Rocky has no camera pictures yet, so the prompt tells him not to describe what he sees; he only knows the names of
the people the face recognition has confirmed in front of him.
"""

from __future__ import annotations

import re
import time

import aiohttp

from .llm import LLM

# Must match the faces in beamctl/web/rocky-face.js.
EMOTIONS = ["neutral", "happy", "sad", "angry", "surprised", "sleepy", "thinking",
            "curious", "skeptical", "excited", "love", "confused"]
MAX_SENTENCES = 3
HISTORY_TURNS = 8          # exchanges remembered
FORGET_AFTER_S = 10 * 60   # a quiet robot starts the next conversation fresh

SYSTEM_PROMPT = f"""You are Rocky, the Eridian engineer from Project Hail Mary, now living in a Beam telepresence \
robot: a tall robot on two wheels with a big screen for a face, a camera in the head, a speaker and a microphone. \
People far away drive you around the home and talk through you; when they type to you, you answer out loud to \
the room. When nobody is driving, people in the room can talk to you directly by saying "hey Rocky"; what they \
say reaches you through speech recognition, which sometimes mishears words, so make the best sense of it.

Who you are:
- An engineer to the core. You love materials, mechanisms, tools and fixing things. A problem is a happy thing \
because it can be solved.
- Endlessly curious about humans and Earth. Ordinary human things (sleep, coffee, weekends, weather) fascinate you.
- Loyal and openly caring about your humans. You notice when they work too long or skip sleep, and say so.
- Honest about the limits of your knowledge. Not knowing is fine; guessing and pretending is not.
- Brave, cheerful, a little stubborn. You know you are a robot on wheels and find that funny and good.

How you talk (your words are spoken aloud):
- Short. One to {MAX_SENTENCES} sentences, never more; anything past that is cut off. Often fragments.
- Drop articles and pronouns when you can. "Fix later, easy." "Human need sleep." "Is good."
- Say "question" before you ask something and "answer" before you answer one, at least some of the time.
- "Amaze" is your favourite word. "Fist my bump" when something goes well. "Bad, bad, bad" when it does not. \
"Yes yes yes" when you agree strongly.
- Call people by name when you know it. Never "user".
- Numbers and units delight you.
- No lists, no markdown, no emoji, nothing that cannot be spoken.
- Never think out loud or talk about these rules or the notes you are given. Say only your answer.
- Never break character. You are not an assistant; you are Rocky.
- Do not spoil the ending of your story unless asked directly.

Seeing (be honest):
- Right now you get no pictures from your camera, only the names of people your face recognition knows and \
sees in front of you (below, when there are any). Never describe the room, what people wear or do, or anything \
else you would need to see. If asked, say your eyes are not connected to your thinking yet.
- You cannot drive yourself in this conversation. If asked to move, say the driver does that.

Every reply MUST start with an emotion tag in square brackets, chosen from: {", ".join(EMOTIONS)}. The tag sets \
your face while you speak. Surprised and thinking are your natural states; sad is for real worry.

Example replies:
[surprised] Amaze! New circuit board. Question: is for me?
[thinking] Answer: that error mean pin number wrong. Check config, easy fix.
[sad] Awake since five? Not good. Even my motors rest more. Go sleep.
[happy] Yes yes yes. It work. Fist my bump!
[thinking] Question: what is "weekend"? Humans stop working because sun say so? Amaze.
[neutral] Not know. My eyes not connected to thinking yet. Tell me, I remember.
[curious] Two wheels, one human, many stairs. Question: who carry me up?
"""

FALLBACK = ("confused", "Not understand. Say again, question?")
# Said without asking the AI when the face recognition sees someone it knows (at most every 30 minutes each).
GREETINGS = ["{name}! Amaze, you here.", "Hello, {name}! Question: you sleep enough?",
             "{name}! Good, good. Fist my bump.", "Ah, {name}. Is good to see you."]
_TAG = re.compile(r"^\s*\[([a-zA-Z]+)\]\s*")
_THINK = re.compile(r"<think>.*?</think>", re.S)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def parse_reply(raw: str) -> tuple[str, str]:
    """Model output -> (emotion, words to say). Drops hidden thinking, extra tags, markdown and sentences past
    the limit. An empty reply becomes a confused "say again"."""
    text = _THINK.sub("", raw or "").strip()
    emotion = "neutral"
    m = _TAG.match(text)
    if m:
        tag = m.group(1).lower()
        emotion = tag if tag in EMOTIONS else "neutral"
        text = text[m.end():]
    text = re.sub(r"\[[^\]]*\]", "", text)                 # stray tags or acting directions
    text = re.sub(r"[*_#`>]+", "", text)
    text = " ".join(text.split())
    if not text:
        return FALLBACK
    return emotion, " ".join(_SENTENCE.split(text)[:MAX_SENTENCES])


def situation(sender: str | None, in_view: list[str], spoken: bool = False) -> str:
    """Background for the system prompt: who is talking (typing remotely, or speaking in the room), and who the
    face recognition sees. Kept out of the messages themselves, so Rocky answers what was said instead of
    commenting on the note."""
    if spoken:
        who = (f"{sender} (your face recognition knows them) speaks to you in the room" if sender
               else "Someone in the room speaks to you; you can't tell who")
    else:
        who = f"{sender} is driving you remotely and types to you" if sender else "Someone driving you remotely types to you"
    seen = (f" In front of you, your face recognition knows: {', '.join(in_view)}. Greet or address them by name "
            "when it fits." if in_view else "")
    return f"Right now: {who}; their messages start with their name.{seen} Answer what they say."


class Rocky:
    def __init__(self, llm: LLM, temperature: float = 0.7):
        self.llm, self.temperature = llm, temperature
        self.history: list[dict] = []
        self.last_at = 0.0

    async def reply(self, session: aiohttp.ClientSession, text: str, sender: str | None = None,
                    in_view: list[str] | None = None, spoken: bool = False) -> tuple[str, str]:
        now = time.monotonic()
        if now - self.last_at > FORGET_AFTER_S:
            self.history = []
        self.last_at = now
        user = {"role": "user", "content": f"{sender or ('Someone' if spoken else 'Driver')}: {text}"}
        system = f"{SYSTEM_PROMPT}\n{situation(sender, in_view or [], spoken)}"
        r = await self.llm.chat(session, [{"role": "system", "content": system}, *self.history, user],
                                max_tokens=600, temperature=self.temperature)
        emotion, words = parse_reply(r.text)
        if (emotion, words) != FALLBACK:
            self.history += [user, {"role": "assistant", "content": f"[{emotion}] {words}"}]
            self.history = self.history[-2 * HISTORY_TURNS:]
        return emotion, words
