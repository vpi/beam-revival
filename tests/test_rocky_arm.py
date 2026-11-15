"""Rocky's arm tool: he may only name one of the robot's gestures, only for people who may control him, and the
robot's own answer (it refuses while a person is at the controls) is what he's told."""

import asyncio
import types

import aiohttp

from beambrain.brain import Brain
from beambrain.greeter import Greeter
from beambrain.people import PeopleStore
from beambrain.rocky import Rocky
from test_home import ScriptedLLM


def arm(gestures=("wave",), answer=(True, "Doing it now.")):
    played = []

    async def play(name):
        played.append(name)
        return answer
    return types.SimpleNamespace(names=lambda: list(gestures), play=play), played


async def test_asked_to_wave_he_waves():
    tools, played = arm()
    llm = ScriptedLLM({"name": "arm", "arguments": {"gesture": "wave"}}, "[happy] Wave wave wave!")
    rocky = Rocky(llm, arm=tools)
    async with aiohttp.ClientSession() as s:
        emotion, words, actions = await rocky.reply(s, "hey Rocky, wave", spoken=True, may_control=True)
    assert played == ["wave"] and actions == [{"tool": "arm", "args": {"gesture": "wave"}, "ok": True}]
    first = llm.calls[0]
    tool = next(t for t in first["tools"] if t["function"]["name"] == "arm")
    assert tool["function"]["parameters"]["properties"]["gesture"]["enum"] == ["wave"]
    assert "never on your own" in first["messages"][0]["content"]


async def test_no_arm_for_people_who_may_not_control_him_nor_without_an_arm():
    tools, played = arm()
    llm = ScriptedLLM("[neutral] Hello.", "[neutral] Hello.")
    async with aiohttp.ClientSession() as s:
        await Rocky(llm, arm=tools).reply(s, "wave", "Visitor", may_control=False)
        await Rocky(llm, arm=arm(gestures=())[0]).reply(s, "wave", "Ann", may_control=True)
    for call in llm.calls:
        assert not any(t["function"]["name"] == "arm" for t in call.get("tools") or [])
        assert "arm tool" not in call["messages"][0]["content"]


async def test_only_his_gestures_and_one_at_a_time_and_a_refusal_is_told_honestly():
    tools, played = arm(answer=(False, "Not done: a person is at the controls."))
    llm = ScriptedLLM([{"name": "arm", "arguments": {"gesture": "backflip"}},
                       {"name": "arm", "arguments": {"gesture": "wave"}},
                       {"name": "arm", "arguments": {"gesture": "wave"}}], "[sad] Not now.")
    rocky = Rocky(llm, arm=tools)
    async with aiohttp.ClientSession() as s:
        _, _, actions = await rocky.reply(s, "wave", "Ann", may_control=True)
    assert played == ["wave"]                                    # the made-up one never reaches the robot
    assert [a["ok"] for a in actions] == [False, False, False]
    told = [m["content"] for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert told == ["Not done: no gesture 'backflip'.", "Not done: a person is at the controls.",
                    "Not done: one gesture at a time."]


async def test_the_brain_asks_the_robot_and_passes_on_its_answer():
    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    sent = []

    async def send(m):
        sent.append(m)
    brain.send = send
    assert brain.arm_tools().names() == []                        # no arm in the robot's status
    brain.telemetry = {"arm": {"present": True, "gestures": ["wave"]}}
    assert brain.arm_tools().names() == ["wave"]
    task = asyncio.create_task(brain.play_gesture("wave"))
    await asyncio.sleep(0.05)
    assert sent == [{"type": "arm_gesture", "name": "wave"}]
    await brain.on_message({"type": "arm_result", "ok": False, "reason": "a person is at the controls"})
    assert await task == (False, "Not done: a person is at the controls.")
