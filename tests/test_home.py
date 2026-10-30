"""Rocky and Home Assistant: HA's MCP server as tools, the tool loop, and who may control the home."""

import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from beambrain.home import HomeAssistant
from beambrain.llm import Reply
from beambrain.rocky import HOME_PROMPT, MAX_CALLS_PER_ROUND, NO_HOME_PROMPT, Rocky

TOOLS = [{"name": "intent__HassTurnOn", "description": "Turns on a device",
          "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
         {"name": "homeassistant__GetLiveContext", "description": "States", "inputSchema": {"type": "object"}}]


@pytest.fixture
async def fake_ha():
    """A stand-in for HA's /api/mcp: lists TOOLS, and "turns on" anything except "Broken Lamp"."""
    state = {"calls": [], "auth": set(), "sse": False}

    async def mcp(request):
        state["auth"].add(request.headers.get("Authorization"))
        rpc = await request.json()
        if state.get("slow"):
            await asyncio.sleep(5)
        if rpc["method"] == "tools/list":
            result = {"tools": TOOLS}
        else:
            state["calls"].append((rpc["params"]["name"], rpc["params"]["arguments"]))
            broken = rpc["params"]["arguments"].get("name") == "Broken Lamp"
            result = {"content": [{"type": "text", "text": "failed" if broken else "done"}], "isError": broken}
        body = json.dumps({"jsonrpc": "2.0", "id": rpc["id"], "result": result})
        if state["sse"]:
            return web.Response(text=f"event: message\ndata: {body}\n\n", content_type="text/event-stream")
        return web.Response(text=body, content_type="application/json")
    app = web.Application()
    app.router.add_post("/api/mcp", mcp)
    server = TestServer(app)
    await server.start_server()
    state["url"] = str(server.make_url("/api/mcp"))
    yield state
    await server.close()


async def test_ha_tools_and_calls(fake_ha):
    ha = HomeAssistant(fake_ha["url"], "secret-token")
    async with aiohttp.ClientSession() as s:
        tools = await ha.tools(s)
        assert tools[0] == {"type": "function", "function": {"name": "intent__HassTurnOn",
                                                             "description": "Turns on a device",
                                                             "parameters": TOOLS[0]["inputSchema"]}}
        assert await ha.call(s, "intent__HassTurnOn", {"name": "Desk Lamp"}) == (True, "done")
        assert await ha.call(s, "intent__HassTurnOn", {"name": "Broken Lamp"}) == (False, "failed")
        fake_ha["sse"] = True                                      # HA may answer as an event stream
        assert await ha.call(s, "intent__HassTurnOn", {"name": "Desk Lamp"}) == (True, "done")
        ok, _ = await ha.call(s, "made_up_tool", {})              # only HA's own tools, never sent
        assert not ok
    assert fake_ha["auth"] == {"Bearer secret-token"}
    assert [c[0] for c in fake_ha["calls"]] == ["intent__HassTurnOn"] * 3


async def test_ha_down_is_a_failed_action_not_a_crash():
    ha = HomeAssistant("http://127.0.0.1:9/api/mcp", "t", timeout_s=2)
    ha._tools = [{"type": "function", "function": {"name": "intent__HassTurnOn"}}]
    async with aiohttp.ClientSession() as s:
        ok, text = await ha.call(s, "intent__HassTurnOn", {"name": "x"})
    assert not ok and text == "Home Assistant didn't answer."


async def test_a_slow_ha_does_not_hold_rocky_up(fake_ha, monkeypatch):
    import beambrain.home as home
    monkeypatch.setattr(home, "LIST_TIMEOUT", aiohttp.ClientTimeout(total=0.3))
    ha = HomeAssistant(fake_ha["url"], "t")
    fake_ha["slow"] = True
    async with aiohttp.ClientSession() as s:
        with pytest.raises(Exception):
            await ha.tools(s)                                      # gives up after a short wait...
        t0 = time.monotonic()
        with pytest.raises(home.HomeError):
            await ha.tools(s)                                      # ...then doesn't wait at all for a while
        assert time.monotonic() - t0 < 0.1
        fake_ha["slow"] = False
        ha._retry_at = 0
        assert await ha.tools(s)                                   # back: the list again
        fake_ha["slow"], ha._tools_at = True, 0                    # due a re-read, but HA is slow:
        assert await ha.tools(s)                                   # the last list still serves


class ScriptedLLM:
    """Answers with the given replies in turn; a dict is a tool call, a str is text."""
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    async def chat(self, session, messages, tools=None, **kw):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        nxt = self.replies.pop(0)
        if isinstance(nxt, str):
            return Reply(nxt)
        calls = nxt if isinstance(nxt, list) else [nxt]
        raw_calls = [{"id": f"c{i}", "type": "function",
                      "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                     for i, c in enumerate(calls)]
        return Reply("", calls, raw={"choices": [{"message": {"content": None, "tool_calls": raw_calls}}]})


async def test_rocky_switches_a_lamp_then_answers_in_character(fake_ha):
    llm = ScriptedLLM({"name": "intent__HassTurnOn", "arguments": {"name": "Desk Lamp"}},
                      "[happy] Lamp on! Fist my bump.")
    rocky = Rocky(llm, home=HomeAssistant(fake_ha["url"], "t"))
    async with aiohttp.ClientSession() as s:
        emotion, words, actions = await rocky.reply(s, "turn on desk lamp", spoken=True, may_control=True)
    assert (emotion, words) == ("happy", "Lamp on! Fist my bump.")
    assert actions == [{"tool": "intent__HassTurnOn", "args": {"name": "Desk Lamp"}, "ok": True}]
    assert fake_ha["calls"] == [("intent__HassTurnOn", {"name": "Desk Lamp"})]
    first, second = llm.calls
    assert first["tools"] and HOME_PROMPT in first["messages"][0]["content"]
    tool_msg = second["messages"][-1]
    assert tool_msg == {"role": "tool", "tool_call_id": "c0", "content": "done"}
    assert [m["role"] for m in rocky.history] == ["user", "assistant"]     # tool chatter isn't remembered


async def test_a_driver_not_on_the_list_gets_no_home_tools(fake_ha):
    llm = ScriptedLLM("[neutral] Only household can ask. Sorry.")
    rocky = Rocky(llm, home=HomeAssistant(fake_ha["url"], "t"))
    async with aiohttp.ClientSession() as s:
        *_, actions = await rocky.reply(s, "turn on desk lamp", "Mallory", may_control=False)
    assert actions == [] and fake_ha["calls"] == []
    msgs = llm.calls[0]["messages"]
    assert llm.calls[0]["tools"] is None and HOME_PROMPT not in msgs[0]["content"]
    assert msgs[-2] == {"role": "system", "content": NO_HOME_PROMPT.strip()}     # just before their message


async def test_tool_loop_is_bounded(fake_ha):
    many = [{"name": "intent__HassTurnOn", "arguments": {"name": f"Lamp {i}"}} for i in range(6)]
    loop = {"name": "intent__HassTurnOn", "arguments": {"name": "Desk Lamp"}}
    llm = ScriptedLLM(many, loop, loop, "[happy] Done.")
    rocky = Rocky(llm, home=HomeAssistant(fake_ha["url"], "t"))
    async with aiohttp.ClientSession() as s:
        _, words, actions = await rocky.reply(s, "all lights on", spoken=True, may_control=True)
    assert words == "Done."
    assert len(fake_ha["calls"]) == MAX_CALLS_PER_ROUND + 2           # capped per round, and in rounds
    assert llm.calls[-1]["tools"] is None                              # the last round must answer in words
    assert sum(m["role"] == "tool" for m in llm.calls[1]["messages"]) == 6   # every call gets an answer


def test_who_may_control_the_home():
    pytest.importorskip("cv2")
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore
    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    brain.home_drivers = {"owner@example.com"}
    assert brain.may_control_home(True, None)                          # anyone in the room
    assert brain.may_control_home(False, "Owner@Example.com")          # an allowed driver
    assert not brain.may_control_home(False, "guest@example.com")
    assert not brain.may_control_home(False, None)                     # unknown driver
