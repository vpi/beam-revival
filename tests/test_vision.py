"""Rocky, step 3: he looks (one picture from the head or floor camera) only when a question needs it."""

import asyncio
import time

import pytest

from beambrain.rocky import BLIND, EYES, LOOK_TOOL, MAX_LOOKS, Rocky
from test_brain_relay import KEY, next_of, rig  # noqa: F401  (rig is a fixture)
from test_home import ScriptedLLM


class Eyes:
    def __init__(self, jpeg="SU1BR0U="):
        self.jpeg, self.asked = jpeg, []

    async def __call__(self, camera):
        self.asked.append(camera)
        return self.jpeg


async def test_he_looks_then_answers_from_the_picture():
    llm = ScriptedLLM({"name": "look", "arguments": {"camera": "floor"}}, "[curious] Answer: one red sock. Amaze.")
    eyes = Eyes()
    rocky = Rocky(llm, eyes=eyes)
    emotion, words, actions = await rocky.reply(None, "is the floor clear?", spoken=True)
    assert (emotion, words) == ("curious", "Answer: one red sock. Amaze.")
    assert eyes.asked == ["floor"] and actions == [{"tool": "look", "args": {"camera": "floor"}, "ok": True}]
    first, second = llm.calls
    assert first["tools"] == [LOOK_TOOL] and EYES in first["messages"][0]["content"]
    *_, tool_msg, picture = second["messages"]
    assert tool_msg["role"] == "tool" and "below" in tool_msg["content"]
    assert picture["role"] == "user"
    assert picture["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,SU1BR0U="}}
    assert all(isinstance(m["content"], str) for m in rocky.history)    # pictures aren't kept


async def test_without_eyes_he_says_so_and_has_no_tools():
    llm = ScriptedLLM("[neutral] Eyes not working now.")
    await Rocky(llm).reply(None, "what do you see?", spoken=True)
    assert llm.calls[0]["tools"] is None and BLIND in llm.calls[0]["messages"][0]["content"]


async def test_no_picture_when_the_camera_is_off():
    llm = ScriptedLLM({"name": "look", "arguments": {"camera": "head"}}, "[sad] Camera dark. Not see.")
    _, _, actions = await Rocky(llm, eyes=Eyes(None)).reply(None, "what do you see?", spoken=True)
    assert actions == [{"tool": "look", "args": {"camera": "head"}, "ok": False}]
    assert llm.calls[1]["messages"][-1]["role"] == "tool"                # no picture message follows


async def test_looks_per_answer_are_limited():
    look = {"name": "look", "arguments": {"camera": "head"}}
    eyes = Eyes()
    llm = ScriptedLLM(look, look, look, "[happy] Seen enough.")
    _, _, actions = await Rocky(llm, eyes=eyes).reply(None, "look again and again", spoken=True)
    assert len(eyes.asked) == MAX_LOOKS
    assert [a["ok"] for a in actions] == [True] * MAX_LOOKS + [False]


async def test_brain_gives_a_fresh_frame_and_tells_the_robot_screen(rig):
    pytest.importorskip("cv2")
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore

    _, _, client = rig
    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    stop = asyncio.Event()
    runner = asyncio.create_task(brain.run(str(client.make_url("/ws")) + f"?role=brain&key={KEY}", stop))
    try:
        robot = await client.ws_connect("/ws?role=robot")
        await next_of(robot, "seen")                                     # the brain itself is connected
        brain.frames["nav"] = ("RkxPT1I=", time.monotonic())
        assert await brain.look("floor") == "RkxPT1I="
        assert (await next_of(robot, "rocky_look"))["camera"] == "floor"
        brain.frames["head"] = ("T0xE", time.monotonic() - 10)           # stale: the camera stopped
        assert await brain.look("head") is None
        with pytest.raises(asyncio.TimeoutError):
            await next_of(robot, "rocky_look", timeout=0.3)              # no sign when nothing was sent
        await robot.close()
    finally:
        stop.set()
        await runner
