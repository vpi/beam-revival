"""Rocky, step 1: typed chat from a driving page -> brain -> in-character reply spoken on the robot with a face."""

import asyncio
import base64

import pytest

from beambrain.rocky import EMOTIONS, FALLBACK, MAX_SENTENCES, Rocky, parse_reply, situation
from test_brain_relay import KEY, next_of, rig  # noqa: F401  (rig is a fixture)


def test_reply_parsing():
    assert parse_reply("[happy] Yes yes yes. It work!") == ("happy", "Yes yes yes. It work!")
    assert parse_reply("  [Surprised]Amaze!") == ("surprised", "Amaze!")
    assert parse_reply("[grumpy] Hm.") == ("neutral", "Hm.")                     # unknown tag: neutral face
    assert parse_reply("No tag at all.") == ("neutral", "No tag at all.")
    assert parse_reply("<think>plan the answer</think>[thinking] **Answer**: two.") == ("thinking", "Answer: two.")
    assert parse_reply("[sad] [sighs] Bad, bad, bad.") == ("sad", "Bad, bad, bad.")  # acting directions dropped
    long = "[happy] One. Two! Three? Four. Five."
    assert parse_reply(long)[1].count(" ") == MAX_SENTENCES - 1                   # cut to the sentence limit
    assert parse_reply("") == FALLBACK and parse_reply("[happy]") == FALLBACK


def test_faces_match_the_robot_screen():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "beamctl" / "web" / "rocky-face.js").read_text()
    for e in EMOTIONS:
        assert f"{e}: {{" in js, e


def test_situation_names_who_types_and_who_is_seen():
    s = situation("Alex", ["Leo", "Mia"])
    assert "Alex is driving you" in s and "knows: Leo, Mia" in s
    assert "face recognition" not in situation(None, [])        # nobody known in view: nothing to comment on


class FakeLLM:
    def __init__(self, text):
        self.text, self.calls = text, []

    async def chat(self, session, messages, **kw):
        self.calls.append(messages)
        from beambrain.llm import Reply
        return Reply(self.text)


async def test_rocky_remembers_the_conversation():
    llm = FakeLLM("[curious] Question: what is coffee?")
    rocky = Rocky(llm)
    assert await rocky.reply(None, "hi", "Alex", []) == ("curious", "Question: what is coffee?", [])
    await rocky.reply(None, "coffee is fuel", "Alex", ["Leo"])
    second = llm.calls[1]
    assert second[0]["role"] == "system" and "Rocky" in second[0]["content"] and "Leo" in second[0]["content"]
    assert [m["role"] for m in second[1:]] == ["user", "assistant", "user"]
    assert second[-1]["content"] == "Alex: coffee is fuel"


async def test_chat_gets_the_drivers_name_and_rocky_reaches_robot_and_drivers(rig):
    _, _, client = rig
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    robot = await client.ws_connect("/ws?role=robot")
    pilot = await client.ws_connect("/ws", headers={"Tailscale-User-Name": "Alex P.",
                                                    "Tailscale-User-Login": "alex@example.com"})
    await asyncio.sleep(0.1)
    await pilot.send_json({"type": "brain", "data": {"kind": "chat", "text": "hello", "sender": "Mallory",
                                                     "login": "mallory@example.com"}})
    cmd = await next_of(brain, "brain_cmd")
    # The server's name and login, not the page's.
    assert cmd["data"] == {"kind": "chat", "text": "hello", "sender": "Alex", "login": "alex@example.com"}

    await brain.send_json({"type": "face", "emotion": "thinking"})
    assert (await next_of(robot, "face"))["emotion"] == "thinking"
    await brain.send_json({"type": "rocky", "sender": "Alex", "heard": "hello", "emotion": "happy", "text": "Hi Alex!"})
    assert (await next_of(pilot, "rocky"))["text"] == "Hi Alex!"
    for ws in (brain, robot, pilot):
        await ws.close()


async def test_no_chat_while_asleep(rig):
    _, ctrl, client = rig
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    pilot = await client.ws_connect("/ws")
    ctrl.go_to_sleep("test")
    await pilot.send_json({"type": "brain", "data": {"kind": "chat", "text": "hello"}})
    assert "asleep" in (await next_of(pilot, "notice"))["text"]
    with pytest.raises(asyncio.TimeoutError):
        await next_of(brain, "brain_cmd", timeout=0.4)
    for ws in (brain, pilot):
        await ws.close()


async def test_brain_answers_a_chat_with_face_speech_and_log(rig):
    pytest.importorskip("cv2")
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore

    class FakeSpeaker:
        def wav(self, text):
            return b"RIFF" + text.encode()

    _, _, client = rig
    brain = Brain(None, PeopleStore(":memory-not-used:"), FakeSpeaker(), Greeter())
    brain.rocky = Rocky(FakeLLM("[excited] Amaze! Fist my bump."))
    stop = asyncio.Event()
    runner = asyncio.create_task(brain.run(str(client.make_url("/ws")) + f"?role=brain&key={KEY}", stop))
    try:
        robot = await client.ws_connect("/ws?role=robot")
        pilot = await client.ws_connect("/ws", headers={"Tailscale-User-Name": "Alex"})
        await next_of(pilot, "people")                    # the brain is connected and has said hello
        await pilot.send_json({"type": "brain", "data": {"kind": "chat", "text": "We fixed it!"}})
        assert (await next_of(robot, "face"))["emotion"] == "thinking"
        say = await next_of(robot, "say")
        assert (say["by"], say["emotion"], say["text"]) == ("rocky", "excited", "Amaze! Fist my bump.")
        assert say["wav"]
        log = await next_of(pilot, "rocky")
        assert (log["sender"], log["heard"]) == ("Alex", "We fixed it!")
        assert "Alex" in brain.rocky.history[0]["content"]
        await robot.close()
        await pilot.close()
    finally:
        stop.set()
        await runner


# --- Fish Audio voice (optional, cloud) -------------------------------------------------

@pytest.fixture
async def fake_fish():
    """A stand-in for api.fish.audio: streams 1.2 s of raw PCM in odd-sized pieces, or 402 "no credit"."""
    import numpy as np
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    t = np.arange(int(1.2 * 24000)) / 24000
    state = {"credit": True, "seen": [], "pcm": (0.08 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes()}

    async def tts(request):
        state["seen"].append((request.headers.get("Authorization"), await request.json()))
        if not state["credit"]:
            return web.json_response({"status": 402, "message": "Insufficient API credit."}, status=402)
        r = web.StreamResponse(headers={"Content-Type": "audio/pcm"})
        await r.prepare(request)
        for i in range(0, len(state["pcm"]), 1001):
            await r.write(state["pcm"][i:i + 1001])
        await r.write_eof()
        return r
    app = web.Application()
    app.router.add_post("/v1/tts", tts)
    server = TestServer(app)
    await server.start_server()
    state["url"] = str(server.make_url("/v1/tts"))
    yield state
    await server.close()


async def test_fish_voice_speaks_and_explains_refusals_without_the_key(fake_fish):
    import aiohttp
    from beambrain.speech import FISH_ROCKY, FishError, FishVoice
    fish = FishVoice("secret-key", url=fake_fish["url"])
    async with aiohttp.ClientSession() as session:
        pieces = [p async for p in fish.stream(session, "Amaze!")]
        assert b"".join(pieces) == fake_fish["pcm"] and all(len(p) % 2 == 0 for p in pieces)   # whole samples
        auth, body = fake_fish["seen"][0]
        assert auth == "Bearer secret-key" and body["reference_id"] == FISH_ROCKY and body["format"] == "pcm"
        fake_fish["credit"] = False
        with pytest.raises(FishError) as e:
            [p async for p in fish.stream(session, "Amaze!")]
    assert "402" in str(e.value) and "credit" in str(e.value) and "secret-key" not in str(e.value)


async def streamed_say(robot):
    """A streamed reply as the robot gets it: the "say", then its sound."""
    say = await next_of(robot, "say")
    pcm = b""
    while not (m := await next_of(robot, "say_audio")).get("end"):
        assert m["stream"] == say["stream"]
        pcm += base64.b64decode(m["pcm"])
    return say, pcm


async def test_voice_switch_and_fallback_to_piper(rig, fake_fish):
    pytest.importorskip("cv2")
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore
    from beambrain.speech import FishVoice

    class FakeSpeaker:
        def wav(self, text):
            return b"RIFFpiper"

    _, _, client = rig
    brain = Brain(None, PeopleStore(":memory-not-used:"), FakeSpeaker(), Greeter())
    brain.rocky = Rocky(FakeLLM("[happy] Is good."))
    brain.fish = FishVoice("k", url=fake_fish["url"])
    stop = asyncio.Event()
    runner = asyncio.create_task(brain.run(str(client.make_url("/ws")) + f"?role=brain&key={KEY}", stop))
    try:
        robot = await client.ws_connect("/ws?role=robot")
        pilot = await client.ws_connect("/ws")
        v = await next_of(pilot, "rocky_voice")
        assert (v["voice"], v["available"]) == ("piper", ["piper", "fish"])

        await pilot.send_json({"type": "brain", "data": {"kind": "voice", "voice": "fish"}})
        assert (await next_of(pilot, "rocky_voice"))["voice"] == "fish"
        late = await client.ws_connect("/ws")                       # a driver who joins later sees the choice
        assert (await next_of(late, "rocky_voice"))["voice"] == "fish"

        await pilot.send_json({"type": "brain", "data": {"kind": "chat", "text": "hi"}})
        say, pcm = await streamed_say(robot)                         # Fish, streamed
        assert say["rate"] == 24000 and "wav" not in say and len(pcm) == len(fake_fish["pcm"])
        assert (await next_of(pilot, "rocky"))["voice"] == "fish"

        fake_fish["credit"] = False                                 # out of credit: Piper, and say why
        await pilot.send_json({"type": "brain", "data": {"kind": "chat", "text": "hi again"}})
        assert "credit" in (await next_of(pilot, "notice"))["text"]
        assert (await next_of(pilot, "rocky"))["voice"] == "piper"
        for ws in (robot, pilot, late):
            await ws.close()
    finally:
        stop.set()
        await runner


def test_streamed_voice_is_levelled_after_a_short_lead():
    import numpy as np
    from beambrain.speech import Leveller
    t = np.arange(24000) / 24000
    quiet = (0.1 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes()   # 1 s, like Fish's Rocky
    lev = Leveller()
    assert lev.feed(quiet[:6000]) == []                            # 0.125 s: still judging how loud it is
    out = lev.feed(quiet[6000:]) + lev.flush()
    assert len(out[0]) == 12000 and sum(map(len, out)) == len(quiet)  # 0.25 s pieces, nothing lost
    y = np.frombuffer(b"".join(out), np.int16) / 32768
    assert 0.15 < np.sqrt(np.mean(y[np.abs(y) > 0.01] ** 2)) < 0.2  # about Piper's loudness
    assert Leveller().flush() == []


def test_quiet_voice_is_levelled_without_clipping():
    import io
    import wave
    import numpy as np
    from beambrain.speech import level

    def wav(samples):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes((samples * 32767).astype(np.int16).tobytes())
        return buf.getvalue()

    t = np.arange(24000) / 24000
    quiet = 0.1 * np.sin(2 * np.pi * 220 * t)                     # like Fish's Rocky: RMS ~0.07
    quiet[5000] = 0.54                                              # one sharp peak
    with wave.open(io.BytesIO(level(wav(quiet)))) as w:
        y = np.frombuffer(w.readframes(w.getnframes()), np.int16) / 32768
    assert 0.15 < np.sqrt(np.mean(y[np.abs(y) > 0.01] ** 2)) < 0.2  # about Piper's loudness
    assert np.abs(y).max() < 1.0                                    # the peak is rounded off, not clipped
    assert level(b"RIFFnot-a-wav") == b"RIFFnot-a-wav"


async def test_voice_choice_survives_a_restart_and_greetings_use_it(rig, fake_fish, tmp_path):
    pytest.importorskip("cv2")
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore
    from beambrain.rocky import GREETINGS
    from beambrain.speech import FishVoice

    class FakeSpeaker:
        def wav(self, text):
            return b"RIFFpiper"

    def new_brain():
        b = Brain(None, PeopleStore(":memory-not-used:"), FakeSpeaker(), Greeter(phrases=GREETINGS))
        b.rocky, b.fish = Rocky(FakeLLM("[happy] Hi.")), FishVoice("k", url=fake_fish["url"])
        b.settings_path = tmp_path / "rocky.json"
        b.load_settings()
        return b

    first = new_brain()
    assert first.rocky_voice == "piper"                         # nothing saved yet
    await first.on_command({"kind": "voice", "voice": "fish"})  # (not connected: messages go nowhere)
    second = new_brain()                                        # the brain restarts
    assert second.rocky_voice == "fish"

    _, _, client = rig
    stop = asyncio.Event()
    runner = asyncio.create_task(second.run(str(client.make_url("/ws")) + f"?role=brain&key={KEY}", stop))
    try:
        robot = await client.ws_connect("/ws?role=robot")
        await next_of(robot, "frames")                          # the brain is connected
        text = second.greeter.maybe_greet("Vera")
        assert "Vera" in text and text in [g.format(name="Vera") for g in GREETINGS]
        await second.greet(text)
        say, pcm = await streamed_say(robot)
        assert (say["by"], say["emotion"], say["text"]) == ("rocky", "happy", text)
        assert pcm and "stream" in say                                  # Fish, not Piper
        await robot.close()
    finally:
        stop.set()
        await runner


def test_personality_is_editable_but_the_rules_stay():
    from beambrain.rocky import BLIND, EYES, PERSONALITY, system_prompt
    mine = system_prompt("You are Captain Rocky, a pirate robot. Say arr.", eyes=False)
    assert mine.startswith("You are Captain Rocky") and PERSONALITY not in mine
    assert "MUST start with an emotion tag" in mine and BLIND in mine and "telepresence robot" in mine
    assert system_prompt("  \n", eyes=True).startswith(PERSONALITY) and EYES in system_prompt("", eyes=True)


async def test_personality_from_the_driving_page(rig, tmp_path):
    pytest.importorskip("cv2")
    import json
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore
    from beambrain.rocky import PERSONALITY

    class FakeSpeaker:
        def wav(self, text):
            return b"RIFFpiper"

    def new_brain():
        b = Brain(None, PeopleStore(":memory-not-used:"), FakeSpeaker(), Greeter())
        b.rocky = Rocky(FakeLLM("[happy] Arr."))
        b.settings_path = tmp_path / "rocky.json"
        b.load_settings()
        return b

    pirate = "You are Captain Rocky, a pirate robot. Say arr."
    brain = new_brain()
    _, _, client = rig
    stop = asyncio.Event()
    runner = asyncio.create_task(brain.run(str(client.make_url("/ws")) + f"?role=brain&key={KEY}", stop))
    try:
        robot = await client.ws_connect("/ws?role=robot")
        pilot = await client.ws_connect("/ws")
        p = await next_of(pilot, "rocky_personality")
        assert p["text"] == p["default"] == PERSONALITY and "name" not in p

        brain.rocky.history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "[happy] Old."}]
        await pilot.send_json({"type": "brain", "data": {"kind": "personality", "text": f"  {pirate}\r\n"}})
        assert "changed Rocky's personality" in (await next_of(pilot, "notice"))["text"]
        p = await next_of(pilot, "rocky_personality")
        assert p["text"] == pirate and p["name"] and p["at"]
        assert brain.rocky.history == []                                # a fresh start
        late = await client.ws_connect("/ws")                           # a driver who joins later sees it
        assert (await next_of(late, "rocky_personality"))["text"] == pirate

        await pilot.send_json({"type": "brain", "data": {"kind": "chat", "text": "hi"}})
        await next_of(pilot, "rocky")
        assert brain.rocky.llm.calls[-1][0]["content"].startswith(pirate)
        assert new_brain().rocky.personality == pirate                  # remembered across a restart

        brain.home_drivers = {"owner@example.com"}                      # a household list: only they may
        await pilot.send_json({"type": "brain", "data": {"kind": "personality", "text": "You are a toaster."}})
        assert "Only the household" in (await next_of(pilot, "notice"))["text"]
        assert (await next_of(pilot, "rocky_personality"))["text"] == pirate
        brain.home_drivers = set()

        await pilot.send_json({"type": "brain", "data": {"kind": "personality", "text": ""}})   # back to default
        assert "back to the default" in (await next_of(pilot, "notice"))["text"]
        p = await next_of(pilot, "rocky_personality")
        assert p["text"] == PERSONALITY and "name" not in p
        assert "personality" not in json.loads((tmp_path / "rocky.json").read_text())
        for ws in (robot, pilot, late):
            await ws.close()
    finally:
        stop.set()
        await runner
