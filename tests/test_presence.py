"""Room presence: Home Assistant presence sensors named on places, read by the brain, shown on the driving page
and told to Rocky (never logged by room)."""

import asyncio
import logging
import types

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from beambrain.home import HomeAssistant, HomeError
from beambrain.places import Places
from beambrain.rocky import Rocky
from test_home import ScriptedLLM
from test_places import make_brain

KITCHEN = "binary_sensor.kit_aa_person_information"
LOUNGE = "binary_sensor.kit_bb_person_information"


def test_places_keep_their_sensors(tmp_path):
    ps = Places(tmp_path / "places.json")
    k = ps.add("kitchen", (1, 0, 0))
    assert ps.set_sensors(k["id"], f" {KITCHEN.upper()} ,{KITCHEN}") is None
    assert ps.sensors() == {k["id"]: [KITCHEN]}
    assert "binary sensor" in ps.set_sensors(k["id"], "light.kitchen")
    assert "binary sensor" in ps.set_sensors(k["id"], "binary_sensor.x/../../api/services")
    assert "charger" in ps.set_sensors("dock", KITCHEN)
    assert Places(tmp_path / "places.json").sensors() == {k["id"]: [KITCHEN]}      # saved
    assert ps.set_sensors(k["id"], "") is None and ps.sensors() == {}


@pytest.fixture
async def fake_states():
    states = {KITCHEN: "on", LOUNGE: "off", "binary_sensor.gone": "unavailable"}
    seen = {"auth": set(), "down": False}

    async def get(request):
        seen["auth"].add(request.headers.get("Authorization"))
        if seen["down"]:
            return web.Response(status=502)
        eid = request.match_info["eid"]
        if eid not in states:
            return web.json_response({"message": "Entity not found."}, status=404)
        return web.json_response({"entity_id": eid, "state": states[eid]})
    app = web.Application()
    app.router.add_get("/api/states/{eid}", get)
    server = TestServer(app)
    await server.start_server()
    yield HomeAssistant(str(server.make_url("/api/mcp")), "secret-token"), states, seen
    await server.close()


async def test_states_from_home_assistant(fake_states):
    ha, states, seen = fake_states
    async with aiohttp.ClientSession() as s:
        got = await ha.states(s, [KITCHEN, LOUNGE, "binary_sensor.gone", "binary_sensor.never", "light.x"])
        assert got == {KITCHEN: True, LOUNGE: False, "binary_sensor.gone": None, "binary_sensor.never": None}
        assert seen["auth"] == {"Bearer secret-token"}
        seen["down"] = True
        with pytest.raises(HomeError):
            await ha.states(s, [KITCHEN])


async def test_brain_shows_who_is_where_and_tells_rocky(tmp_path, fake_states, caplog):
    ha, states, seen = fake_states
    brain, sent = make_brain(tmp_path)
    brain.home, brain.ws = ha, object()
    k = brain.places.add("kitchen", (1, 0, 0))
    lounge = brain.places.add("living room", (2, 0, 0))
    brain.places.add("hall", (3, 0, 0))                                    # no sensor: not watched
    await brain.on_command({"kind": "place_sensors", "id": k["id"], "sensors": KITCHEN})
    await brain.on_command({"kind": "place_sensors", "id": lounge["id"], "sensors": LOUNGE})
    brain.PRESENCE_S = 0.05
    with caplog.at_level(logging.INFO, logger="beambrain"):
        async with aiohttp.ClientSession() as s:
            watch = asyncio.create_task(brain.watch_presence(s))
            await asyncio.sleep(0.2)
            here = {p["names"][0]: p.get("here", "-") for p in sent[-1]["places"]}
            assert here == {"dock": "-", "kitchen": True, "living room": False, "hall": "-"}
            assert brain.people_at() == {"seen": ["kitchen"], "watched": ["kitchen", "living room"], "unknown": []}
            states[KITCHEN], states[LOUNGE] = "off", "on"
            await asyncio.sleep(0.2)
            assert brain.people_at()["seen"] == ["living room"]
            seen["down"] = True                                            # HA away: unknown, not "no one"
            await asyncio.sleep(0.2)
            assert brain.people_at()["unknown"] == ["kitchen", "living room"] and brain.people_at()["seen"] == []
            watch.cancel()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "kitchen" not in text and "living room" not in text and "kit_aa" not in text
    assert "reading 2 sensor(s) for 2 place(s)" in text and "didn't answer" in text


async def test_presence_in_rockys_prompt_only_for_those_who_may_send_him():
    at = {"seen": ["kitchen"], "watched": ["kitchen", "living room"], "unknown": []}
    trips = types.SimpleNamespace(names=lambda: ["dock", "kitchen", "living room"], go=None, now=lambda: None,
                                  people=lambda: at)
    llm = ScriptedLLM(*["[happy] Kitchen. Someone there."] * 3)
    rocky = Rocky(llm, trips=trips)
    async with aiohttp.ClientSession() as s:
        await rocky.reply(s, "where is everyone", spoken=True, may_control=True)
        await rocky.reply(s, "where is everyone", "Mallory", may_control=False)
    first, stranger = (c["messages"][0]["content"] for c in llm.calls)
    assert "presence sensors (only at: kitchen, living room) see someone now at: kitchen" in first
    assert "presence sensors" not in stranger

    at.update(seen=[], unknown=["kitchen", "living room"])
    async with aiohttp.ClientSession() as s:
        await rocky.reply(s, "where is everyone", spoken=True, may_control=True)
    assert "aren't answering" in llm.calls[-1]["messages"][0]["content"]


async def test_no_home_assistant_means_no_presence(tmp_path):
    brain, sent = make_brain(tmp_path)
    k = brain.places.add("kitchen", (1, 0, 0))
    await brain.on_command({"kind": "place_sensors", "id": k["id"], "sensors": KITCHEN})
    assert "no Home Assistant" in sent[-2]["text"] and brain.places.sensors()
    assert brain.people_at() is None
