"""Rocky step 2: listening. Wake word, turn detection, the server's rule (only while nobody drives), the brain's
answer to speech, and (when the models are on this machine) the real speech pipeline on a spoken sentence."""

import asyncio
import os
from pathlib import Path

import numpy as np
import pytest

from beambrain.ears import SAMPLE_RATE, Segmenter, Wake
from test_brain_relay import KEY, next_of, rig  # noqa: F401  (rig is a fixture)
from test_rocky import FakeLLM


async def next_audio(ws, timeout=2.0):
    """The next binary (microphone) message, skipping telemetry and other text."""
    from aiohttp import WSMsgType

    async def loop():
        while True:
            m = await ws.receive()
            if m.type == WSMsgType.BINARY:
                return m.data
    return await asyncio.wait_for(loop(), timeout)


async def listen_state(ws, on):
    """Wait until the server says listening is on / off (a robot page first hears "off" when it connects)."""
    while (await next_of(ws, "listen"))["on"] is not on:
        pass


def test_wake_word():
    w = Wake(awake_s=60)
    assert w.heard("What a nice day.", now=0) == ("ignore", "")              # not for him
    assert w.heard("Rocky is a fun name.", now=1) == ("ignore", "")          # his name alone doesn't wake him
    assert w.heard("Hey, Rocky!", now=2) == ("wake", "")
    assert w.heard("What's a weekend?", now=30) == ("chat", "What's a weekend?")   # awake: no name needed
    assert w.heard("And coffee?", now=89) == ("chat", "And coffee?")         # each exchange keeps him awake
    assert w.heard("Hello?", now=150) == ("ignore", "")                      # a minute of quiet: asleep again
    assert w.heard("hey rocket, how tall are you", now=200) == ("chat", "how tall are you")   # a common mishearing
    w.sleep()
    assert not w.awake


class FakeVAD:
    """Loud = speech."""
    def reset(self):
        pass

    def __call__(self, chunk):
        return 0.9 if np.abs(chunk).max() > 0.1 else 0.05


def audio(*parts):
    """("speech"|"quiet", seconds) parts -> 16 kHz float audio."""
    out = []
    for kind, s in parts:
        n = int(s * SAMPLE_RATE)
        out.append(0.3 * np.sin(np.arange(n) * 0.2) if kind == "speech" else np.zeros(n))
    return np.concatenate(out).astype(np.float32)


def feed(seg, x):
    turns = []
    for i in range(0, len(x), 1600):
        t = seg.push(x[i:i + 1600])
        if t is not None:
            turns.append(t)
    return turns


def test_a_turn_ends_when_the_speaker_sounds_finished():
    seg = Segmenter(FakeVAD(), judge=lambda a: 0.9)
    turns = feed(seg, audio(("quiet", 0.5), ("speech", 1.0), ("quiet", 0.6)))
    assert len(turns) == 1 and 1.0 <= len(turns[0]) / SAMPLE_RATE < 1.6    # with a little pre-roll and pause
    assert "sounds finished" in seg.why


def test_a_pause_mid_sentence_waits_and_a_click_is_ignored():
    seg = Segmenter(FakeVAD(), judge=lambda a: 0.1)                        # "not finished yet"
    turns = feed(seg, audio(("speech", 1.0), ("quiet", 1.0), ("speech", 0.5), ("quiet", 3.0)))
    assert len(turns) == 1 and len(turns[0]) / SAMPLE_RATE > 2.4            # one turn, the pause inside it
    assert "quiet" in seg.why
    seg = Segmenter(FakeVAD(), judge=lambda a: 0.9)
    assert feed(seg, audio(("speech", 0.1), ("quiet", 1.0))) == []          # too short for words


async def test_microphone_goes_to_the_brain_only_while_nobody_drives(rig):
    _, _, client = rig
    robot = await client.ws_connect("/ws?role=robot")
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    await robot.send_bytes(b"\0\0" * 1600)
    with pytest.raises(asyncio.TimeoutError):                               # the brain hasn't asked for it
        await next_audio(brain, timeout=0.3)

    await brain.send_json({"type": "ears", "on": True})
    await listen_state(robot, True)
    await listen_state(brain, True)
    await robot.send_bytes(b"\1\0" * 1600)
    assert await next_audio(brain) == b"\1\0" * 1600

    pilot = await client.ws_connect("/ws")                                   # a driver connects: stop at once
    await listen_state(robot, False)
    await listen_state(brain, False)
    await robot.send_bytes(b"\2\0" * 1600)                                   # a late block isn't passed on
    with pytest.raises(asyncio.TimeoutError):
        await next_audio(brain, timeout=0.3)

    await pilot.close()                                                      # the driver leaves: listening again
    await listen_state(robot, True)
    for ws in (robot, brain):
        await ws.close()


class FakeEars:
    def __init__(self):
        self.fed, self.resets = [], 0
        self.thread = type("T", (), {"is_alive": staticmethod(lambda: True)})()

    def feed(self, pcm):
        self.fed.append(pcm)

    def reset(self):
        self.resets += 1


async def test_brain_answers_speech_and_ignores_what_isnt_for_it(rig):
    pytest.importorskip("cv2")
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore
    from beambrain.rocky import Rocky

    _, _, client = rig
    llm = FakeLLM("[thinking] Answer: two days, no work.")
    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    brain.rocky, brain.ears = Rocky(llm), FakeEars()
    stop = asyncio.Event()
    runner = asyncio.create_task(brain.run(str(client.make_url("/ws")) + f"?role=brain&key={KEY}", stop))
    try:
        robot = await client.ws_connect("/ws?role=robot")
        await listen_state(robot, True)
        await robot.send_bytes(b"\5\0" * 1600)
        for _ in range(50):
            if brain.ears.fed:
                break
            await asyncio.sleep(0.05)
        assert brain.ears.fed == [b"\5\0" * 1600] and brain.listening

        await brain.on_heard("The kettle is on.")                           # not for Rocky: nothing happens
        await brain.on_heard("Hey Rocky, what is a weekend?")
        assert (await next_of(robot, "rocky_awake"))["seconds"] == 60
        assert (await next_of(robot, "face"))["emotion"] == "thinking"
        say = await next_of(robot, "say")
        assert (say["by"], say["text"]) == ("rocky", "Answer: two days, no work.")
        assert len(llm.calls) == 1 and llm.calls[0][-1]["content"] == "Someone: what is a weekend?"
        assert "in the room" in llm.calls[0][0]["content"]

        brain.in_view = ["Leo"]                                              # one known face: that's who's talking
        await asyncio.sleep(0.1)
        await brain.on_heard("And what is coffee?")
        await next_of(robot, "say")
        assert llm.calls[1][-1]["content"] == "Leo: And what is coffee?"

        pilot = await client.ws_connect("/ws")                               # a driver: Rocky stops listening
        await listen_state(robot, False)
        await asyncio.sleep(0.1)
        assert not brain.listening and brain.ears.resets == 1 and not brain.wake.awake
        await brain.on_heard("Hey Rocky, are you there?")
        assert len(llm.calls) == 2
        await pilot.close()
        await robot.close()
    finally:
        stop.set()
        await runner


MODELS = Path(os.environ.get("BEAM_BRAIN_MODELS", Path.home() / "beam-brain" / "models"))


@pytest.mark.skipif(not (MODELS / "whisper").exists() or not (MODELS / "silero_vad.onnx").exists(),
                    reason="speech models not downloaded (they are on the brain host)")
def test_real_speech_pipeline_hears_a_spoken_question():
    """Piper says it, the real VAD, Smart Turn and Whisper must hear it, as the robot's 100 ms blocks."""
    pytest.importorskip("faster_whisper")
    from beambrain.ears import SileroVAD, SmartTurn, Transcriber
    from beambrain.speech import Speaker
    import io
    import wave
    wav = Speaker(MODELS).wav("Hey Rocky, what is a weekend?")
    if wav is None:
        pytest.skip("no Piper voice")
    with wave.open(io.BytesIO(wav)) as w:
        rate, x = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), np.int16) / 32768
    x16 = np.interp(np.arange(0, len(x), rate / SAMPLE_RATE), np.arange(len(x)), x).astype(np.float32)
    x16 = np.concatenate([np.zeros(SAMPLE_RATE // 2), x16, np.zeros(SAMPLE_RATE * 3)]).astype(np.float32)
    seg = Segmenter(SileroVAD(MODELS), SmartTurn(MODELS))
    turns = feed(seg, x16)
    assert 1 <= len(turns) <= 2                    # "Hey Rocky," may end a turn of its own at the comma's pause
    stt, wake = Transcriber(MODELS), Wake()
    heard = [wake.heard(stt(t)) for t in turns]
    assert heard[0][0] in ("wake", "chat") and heard[-1][0] == "chat" and "weekend" in heard[-1][1].lower(), heard


async def test_name_alone_then_the_question_gets_one_answer(rig):
    pytest.importorskip("cv2")
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore
    from beambrain.rocky import Rocky

    _, _, client = rig
    llm = FakeLLM("[happy] Two days, no work.")
    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    brain.rocky, brain.ears = Rocky(llm), FakeEars()
    brain.WAKE_WAIT_S = 0.3
    stop = asyncio.Event()
    runner = asyncio.create_task(brain.run(str(client.make_url("/ws")) + f"?role=brain&key={KEY}", stop))
    try:
        robot = await client.ws_connect("/ws?role=robot")
        await listen_state(robot, True)
        while not brain.listening:
            await asyncio.sleep(0.02)
        await brain.on_heard("Hey Rocky.")                    # a breath after his name...
        await brain.on_heard("What is a weekend?")            # ...then the question
        say = await next_of(robot, "say")
        assert say["text"] == "Two days, no work."            # answered, without a "Question?" first
        with pytest.raises(asyncio.TimeoutError):
            await next_of(robot, "say", timeout=0.6)
        await brain.on_heard("Hey Rocky!")                    # his name and nothing else
        assert (await next_of(robot, "say"))["text"] == "Question?"
        await robot.close()
    finally:
        stop.set()
        await runner


async def test_phone_switch_turns_rockys_microphone_off_and_on(tmp_path):
    """The switch on the phone stops listening at once, tells the room, and is remembered after a restart."""
    from aiohttp.test_utils import TestClient, TestServer
    from beamctl.server import Config, MotorController, build_app
    from beamctl.sim import SimulatedBoard

    async def start():
        board = SimulatedBoard()
        ctrl = MotorController(Config(device=await board.start(), brain_key=KEY,
                                      sleep_state=str(tmp_path / "sleep.json")))
        client = TestClient(TestServer(build_app(ctrl)))
        await client.start_server()
        return board, client

    board, client = await start()
    robot = await client.ws_connect("/ws?role=robot")
    panel = await client.ws_connect("/ws?role=panel")
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    await brain.send_json({"type": "ears", "on": True})
    await listen_state(robot, True)
    status = await next_of(panel, "ears_status")
    while not status["available"]:
        status = await next_of(panel, "ears_status")
    assert status["muted"] is False

    await panel.send_json({"type": "ears_mute", "muted": True})               # someone in the room taps it
    await listen_state(robot, False)
    await listen_state(brain, False)
    assert (await next_of(panel, "ears_status"))["muted"] is True
    assert (await next_of(robot, "ears_status"))["muted"] is True            # the robot screen says so
    await robot.send_bytes(b"\1\0" * 1600)
    with pytest.raises(asyncio.TimeoutError):
        await next_audio(brain, timeout=0.3)
    for ws in (robot, panel, brain):
        await ws.close()
    await client.close()
    await board.stop()

    board, client = await start()                                             # the robot restarts: still off
    robot = await client.ws_connect("/ws?role=robot")
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    await brain.send_json({"type": "ears", "on": True})
    assert (await next_of(robot, "ears_status"))["muted"] is True
    panel = await client.ws_connect("/ws?role=panel")
    await panel.send_json({"type": "ears_mute", "muted": False})              # and back on
    await listen_state(robot, True)
    for ws in (robot, panel, brain):
        await ws.close()
    await client.close()
    await board.stop()