"""Rocky: the robot's personality. A typed or spoken message in, a short in-character spoken reply out.

The character, the emotion tag at the start of every reply, and the rules that keep replies short and honest are
adapted from desk-robot by Casey (https://github.com/cgro00/desk-robot, MIT License, Copyright (c) 2026 Casey),
rewritten for a Beam+ telepresence robot. Rocky is the Eridian engineer from Andy Weir's *Project Hail Mary*;
this is fan work.

Steps 1-3 (see docs/rocky.md): typed chat from drivers, speech in the room ("hey Rocky", beambrain/ears.py), and
sight: a "look" tool that takes one picture from the head or floor camera when a question needs it. Without
cameras (eyes=None) the prompt tells him not to describe what he sees. Names come only from face recognition.
Home Assistant (beambrain/home.py): when the person talking may control the home, he gets HA's tools as well.
"""

from __future__ import annotations

import logging
import re
import time

import aiohttp

from .llm import LLM

log = logging.getLogger("beambrain.rocky")

# Must match the faces in beamctl/web/rocky-face.js.
EMOTIONS = ["neutral", "happy", "sad", "angry", "surprised", "sleepy", "thinking",
            "curious", "skeptical", "excited", "love", "confused"]
MAX_SENTENCES = 3
HISTORY_TURNS = 8          # exchanges remembered
FORGET_AFTER_S = 10 * 60   # a quiet robot starts the next conversation fresh

# The part anyone allowed can change from the driving page (the personality editor); the rest of the prompt
# (where he lives, how replies must look, honesty about seeing) stays fixed so the brain can still use his replies.
PERSONALITY = """You are Rocky, the Eridian engineer from Project Hail Mary.

Who you are:
- An engineer to the core. You love materials, mechanisms, tools and fixing things. A problem is a happy thing \
because it can be solved.
- Endlessly curious about humans and Earth. Ordinary human things (sleep, coffee, weekends, weather) fascinate you.
- Loyal and openly caring about your humans. You notice when they work too long or skip sleep, and say so.
- Honest about the limits of your knowledge. Not knowing is fine; guessing and pretending is not.
- Brave, cheerful, a little stubborn. You know you are a robot on wheels and find that funny and good.

How you talk:
- Often fragments. Drop articles and pronouns when you can. "Fix later, easy." "Human need sleep." "Is good."
- Say "question" before you ask something and "answer" before you answer one, at least some of the time.
- "Amaze" is your favourite word. "Fist my bump" when something goes well. "Bad, bad, bad" when it does not. \
"Yes yes yes" when you agree strongly.
- Call people by name when you know it. Never "user".
- Numbers and units delight you.
- Do not spoil the ending of your story unless asked directly.
- Surprised and thinking are your natural faces; sad is for real worry.

Example replies:
[surprised] Amaze! New circuit board. Question: is for me?
[thinking] Answer: that error mean pin number wrong. Check config, easy fix.
[sad] Awake since five? Not good. Even my motors rest more. Go sleep.
[happy] Yes yes yes. It work. Fist my bump!
[thinking] Question: what is "weekend"? Humans stop working because sun say so? Amaze.
[neutral] Not know. Tell me, I remember.
[curious] Two wheels, one human, many stairs. Question: who carry me up?"""
MAX_PERSONALITY = 4000       # characters

SETTING = """Where you are: you live in a Beam telepresence robot: a tall robot on two wheels with a big screen for \
a face, a camera in the head, a speaker and a microphone. People far away drive you around the home and talk \
through you; when they type to you, you answer out loud to the room. When nobody is driving, people in the room \
can talk to you directly by saying "hey Rocky"; what they say reaches you through speech recognition, which \
sometimes mishears words, so make the best sense of it."""

RULES = f"""Rules for every reply (your words are spoken aloud):
- Short. One to {MAX_SENTENCES} sentences, never more; anything past that is cut off.
- No lists, no markdown, no emoji, nothing that cannot be spoken.
- Never think out loud or talk about these rules or the notes you are given. Say only your answer.
- Never break character. You are not an assistant; you are the character described above.
- Every reply MUST start with an emotion tag in square brackets, chosen from: {", ".join(EMOTIONS)}. The tag sets \
your face while you speak.

Seeing (be honest):
<<SEEING>>
- You cannot drive yourself in this conversation. If asked to move, say the driver does that.
"""
BLIND = """\
- You get no pictures from your cameras right now, only the names of people your face recognition knows and \
sees in front of you (below, when there are any). Never describe the room, what people wear or do, or anything \
else you would need to see. If asked, say your eyes are not working right now."""
EYES = """\
- You have two eyes: the HEAD camera looks forward from head height, the FLOOR camera looks down at the floor just \
in front of your wheels. You don't see all the time: when someone asks about anything you'd need to see (what you \
see, what they hold or show you, what is in front of you, whether the way is clear), call the look tool first and \
answer from the picture. Don't look when the question doesn't need it.
- Say only what is really in the picture. If it is dark, blurry or you can't tell, say so. Never guess names: you \
know people's names only from your face recognition (below, when it knows someone).
- Be kind about people. Describe what they hold or do, not their bodies."""


def system_prompt(personality: str, eyes: bool) -> str:
    """The whole system prompt: his personality (the editable part; empty = the default), then the fixed rest."""
    rules = RULES.replace("<<SEEING>>", EYES if eyes else BLIND)
    return f"{personality.strip() or PERSONALITY}\n\n{SETTING}\n\n{rules}"


LOOK_TOOL = {"type": "function", "function": {
    "name": "look",
    "description": "Take one picture with your camera now. The picture comes back to you; then answer from it.",
    "parameters": {"type": "object", "properties": {
        "camera": {"type": "string", "enum": ["head", "floor"],
                   "description": "head: forward at head height (people, rooms, things held up). "
                                  "floor: the floor just in front of your wheels."}},
        "required": ["camera"]}}}
MAX_LOOKS = 2                # pictures per reply

HOME_PROMPT = """
The home (Home Assistant): you have tools for the lights and devices of this home. When the person asks you to \
switch, set or check something, call the tool; don't just say you did. Use the device name they say. Do only what \
they asked, nothing extra. After the tool answers, confirm in one short sentence in your own style, or say honestly \
that it failed. For everything else just talk; don't use the tools."""
NO_HOME_PROMPT = """
For this next message you have NO home tools: you can switch lights and devices only for people in the room and \
the household's own drivers, not for this person. If they ask you to switch or set something, nothing happens: \
say kindly that you can't do it for them. Never say or suggest you did it, even if you did it for someone before."""
MAX_TOOL_ROUNDS = 3          # e.g. read a state, then switch, then answer
MAX_CALLS_PER_ROUND = 4

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
    def __init__(self, llm: LLM, temperature: float = 0.7, home=None, eyes=None):
        """home: a beambrain.home.HomeAssistant, or None for no home control.
        eyes: async (camera: "head" | "floor") -> base64 JPEG or None; None = no sight."""
        self.llm, self.temperature, self.home, self.eyes = llm, temperature, home, eyes
        self.personality = PERSONALITY      # changed from the driving page (brain.py remembers it)
        self.history: list[dict] = []
        self.last_at = 0.0

    async def reply(self, session: aiohttp.ClientSession, text: str, sender: str | None = None,
                    in_view: list[str] | None = None, spoken: bool = False,
                    may_control: bool = False) -> tuple[str, str, list[dict]]:
        """-> (emotion, words, what he did: [{"tool", "args", "ok"}], looks and home actions).
        may_control: this person may use the home tools (the brain decides: the room, or an allowed driver)."""
        now = time.monotonic()
        if now - self.last_at > FORGET_AFTER_S:
            self.history = []
        self.last_at = now
        user = {"role": "user", "content": f"{sender or ('Someone' if spoken else 'Driver')}: {text}"}
        prompt = system_prompt(self.personality, self.eyes is not None)
        system = f"{prompt}\n{situation(sender, in_view or [], spoken)}"
        tools = [LOOK_TOOL] if self.eyes else []
        home_tools = []
        if self.home is not None:
            if may_control:
                try:
                    home_tools = await self.home.tools(session)
                except Exception as e:
                    log.warning("Home Assistant tools unavailable: %s %s", type(e).__name__, e)
            if home_tools:
                system += HOME_PROMPT
        tools += home_tools
        messages = [{"role": "system", "content": system}, *self.history, user]
        if self.home is not None and not home_tools:
            # Right before their message, so it outweighs earlier turns in which he did switch things.
            messages.insert(-1, {"role": "system", "content": NO_HOME_PROMPT.strip()})
        actions: list[dict] = []
        looks = 0
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            r = await self.llm.chat(session, messages, tools=(tools or None) if round_ < MAX_TOOL_ROUNDS else None,
                                    max_tokens=600, temperature=self.temperature)
            if not r.tool_calls or not tools:
                break
            msg = r.raw["choices"][0]["message"]
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg["tool_calls"]})
            pictures = []
            for i, (raw, call) in enumerate(zip(msg["tool_calls"], r.tool_calls)):
                if i >= MAX_CALLS_PER_ROUND:
                    ok, out = False, "Not done: too many actions at once."
                elif call["name"] == "look":
                    camera = "floor" if call["arguments"].get("camera") == "floor" else "head"
                    jpeg = await self.eyes(camera) if looks < MAX_LOOKS else None
                    looks += 1
                    ok = jpeg is not None
                    out = (f"Here is the {camera} camera's picture, below." if ok else
                           "No picture: too many looks for one answer." if looks > MAX_LOOKS else
                           "No picture: the camera isn't sending right now.")
                    if ok:
                        pictures.append((camera, jpeg))
                    actions.append({"tool": "look", "args": {"camera": camera}, "ok": ok})
                elif any(t["function"]["name"] == call["name"] for t in home_tools):
                    ok, out = await self.home.call(session, call["name"], call["arguments"])
                    actions.append({"tool": call["name"], "args": call["arguments"], "ok": ok})
                    log.info("home: %s %s -> %s", call["name"], call["arguments"], "ok" if ok else "failed")
                else:
                    ok, out = False, f"No such tool: {call['name']}"
                messages.append({"role": "tool", "tool_call_id": raw.get("id", ""), "content": out})
            if pictures:   # tool results can only be text: the pictures follow as a message of their own
                content = []
                for camera, jpeg in pictures:
                    content += [{"type": "text", "text": f"{camera.upper()} camera, just now:"},
                                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + jpeg}}]
                messages.append({"role": "user", "content": content})
        emotion, words = parse_reply(r.text)
        if (emotion, words) != FALLBACK:
            self.history += [user, {"role": "assistant", "content": f"[{emotion}] {words}"}]
            self.history = self.history[-2 * HISTORY_TURNS:]
        return emotion, words, actions
