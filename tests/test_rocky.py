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
    assert await rocky.reply(None, "hi", "Alex", []) == ("curious", "Question: what is coffee?")
    await rocky.reply(None, "coffee is fuel", "Alex", ["Leo"])
    second = llm.calls[1]
    assert second[0]["role"] == "system" and "Rocky" in second[0]["content"] and "Leo" in second[0]["content"]
    assert [m["role"] for m in second[1:]] == ["user", "assistant", "user"]
    assert second[-1]["content"] == "Alex: coffee is fuel"


async def test_chat_gets_the_drivers_name_and_rocky_reaches_robot_and_drivers(rig):
    _, _, client = rig
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    robot = await client.ws_connect("/ws?role=robot")
    pilot = await client.ws_connect("/ws", headers={"Tailscale-User-Name": "Alex P."})
    await asyncio.sleep(0.1)
    await pilot.send_json({"type": "brain", "data": {"kind": "chat", "text": "hello", "sender": "Mallory"}})
    cmd = await next_of(brain, "brain_cmd")
    assert cmd["data"] == {"kind": "chat", "text": "hello", "sender": "Alex"}     # the server's name, not the page's

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
    """A stand-in for api.fish.audio: answers WAV, or 402 "no credit" when told to."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    state = {"credit": True, "seen": []}

    async def tts(request):
        state["seen"].append((request.headers.get("Authorization"), await request.json()))
        if not state["credit"]:
            return web.json_response({"status": 402, "message": "Insufficient API credit."}, status=402)
        return web.Response(body=b"RIFF\xff\xff\xff\xffWAVEfmt fake", content_type="audio/wav")
    app = web.Application()
    app.router.add_post("/v1/tts", tts)
    server = TestServer(app)
    await server.start_server()
    state["url"] = str(server.make_url("/v1/tts"))
    yield state
    await server.close()


async def test_fish_voice_speaks_and_explains_refusals_without_the_key(fake_fish):
    from beambrain.speech import FISH_ROCKY, FishError, FishVoice
    fish = FishVoice("secret-key", url=fake_fish["url"])
    assert (await asyncio.to_thread(fish.wav, "Amaze!")).startswith(b"RIFF")
    auth, body = fake_fish["seen"][0]
    assert auth == "Bearer secret-key" and body["reference_id"] == FISH_ROCKY and body["format"] == "wav"
    fake_fish["credit"] = False
    with pytest.raises(FishError) as e:
        await asyncio.to_thread(fish.wav, "Amaze!")
    assert "402" in str(e.value) and "credit" in str(e.value) and "secret-key" not in str(e.value)


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
        assert base64.b64decode((await next_of(robot, "say"))["wav"]).endswith(b"WAVEfmt fake")   # Fish's WAV
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


def test_fish_wav_gets_its_real_length():
    import io
    import wave
    from beambrain.speech import fix_wav_sizes
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\0\0" * 24000)
    streamed = bytearray(buf.getvalue())
    streamed[4:8] = b"\x24\xff\xff\xff"; streamed[40:44] = b"\x00\xff\xff\xff"    # as Fish sends it
    fixed = fix_wav_sizes(bytes(streamed))
    assert fixed == buf.getvalue()
    assert wave.open(io.BytesIO(fixed)).getnframes() == 24000                     # exactly 1 s


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
        say = await next_of(robot, "say")
        assert (say["by"], say["emotion"], say["text"]) == ("rocky", "happy", text)
        assert base64.b64decode(say["wav"]).endswith(b"WAVEfmt fake")   # Fish, not Piper
        await robot.close()
    finally:
        stop.set()
        await runner
