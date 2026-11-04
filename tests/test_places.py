"""Named places on the map: saving them where the robot stands, going there from the driving page or by asking
Rocky, and "stop" said in the room."""

import asyncio
import types

import aiohttp

from beambrain.brain import Brain
from beambrain.greeter import Greeter
from beambrain.people import PeopleStore
from beambrain.places import DOCK, Places, clean_names
from beambrain.rocky import Rocky
from test_home import ScriptedLLM


def test_names_are_tidied():
    assert clean_names("Living Room,  lounge ; living room") == ["living room", "lounge"]
    assert clean_names("Watch TV!") == ["watch tv"]
    assert clean_names(" , ") == []


def test_places_are_kept_and_found(tmp_path):
    ps = Places(tmp_path / "places.json")
    assert [p["id"] for p in ps.summary()] == [DOCK] and ps.find("Charger")["id"] == DOCK
    kitchen = ps.add("Kitchen", (2.0, 1.0, 0.5))
    assert kitchen["names"] == ["kitchen"]
    assert isinstance(ps.add("kitchen, cooking", (0, 0, 0)), str)          # a name already taken
    assert isinstance(ps.add("charger", (0, 0, 0)), str)                   # the dock's too
    tv = ps.add("watch TV", (4.0, 2.0, 3.1))
    assert ps.rename(tv["id"], "watch tv, tv") is None
    assert ps.find("the TV")["id"] == tv["id"] and ps.find("watch tv")["id"] == tv["id"]
    assert ps.rename(DOCK, "dock, charger, home") is None
    again = Places(tmp_path / "places.json")                                # saved
    assert again.find("home")["id"] == DOCK and again.find("kitchen")["x"] == 2.0
    assert again.remove(kitchen["id"]) and again.find("kitchen") is None
    assert again.all_names() == ["dock", "charger", "home", "watch tv", "tv"]


def make_brain(tmp_path):
    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    sent = []
    brain.send = lambda m: asyncio.sleep(0, sent.append(m))
    brain.maps_dir = tmp_path
    return brain, sent


async def test_saving_needs_to_know_where_it_is(tmp_path):
    brain, sent = make_brain(tmp_path)
    await brain.on_command({"kind": "place_add", "names": "kitchen"})
    assert "doesn't know where it is" in sent[-1]["text"] and brain.places.find("kitchen") is None
    brain.localizer = types.SimpleNamespace(pose=(1.5, -0.5, 1.0), state="here")
    await brain.on_command({"kind": "place_add", "names": "kitchen"})
    assert brain.places.find("kitchen")["x"] == 1.5
    assert sent[-1]["type"] == "places" and [p["names"][0] for p in sent[-1]["places"]] == ["dock", "kitchen"]


async def test_going_to_a_place_or_the_dock(tmp_path):
    brain, sent = make_brain(tmp_path)
    brain.localizer = types.SimpleNamespace(pose=(1.5, -0.5, 1.0), state="here")
    kitchen = brain.places.add("kitchen", (3.0, 1.0, 0.0))
    trips = []

    async def run_move(kind, amount=0.0, place=None):
        trips.append((kind, place and place["id"]))
        return "arrived"
    brain.run_move = run_move
    await brain.on_command({"kind": "goto", "id": kitchen["id"]})
    ok, text = await brain.go_to_place("charger")
    await asyncio.sleep(0)
    assert ok and trips == [("goto", kitchen["id"]), ("dock", None)]
    ok, text = await brain.go_to_place("garage")
    assert not ok and "kitchen" in text                                     # names the ones it knows
    brain.localizer.state = "searching"
    ok, text = await brain.go_to_place("kitchen")
    assert not ok and "where it is" in text


async def test_rocky_goes_when_asked_in_the_room():
    went = []

    async def go(name, spoken):
        went.append((name, spoken))
        return True, "Started going to “kitchen”."
    trips = types.SimpleNamespace(names=lambda: ["dock", "charger", "kitchen"], go=go)
    llm = ScriptedLLM({"name": "go_to", "arguments": {"place": "kitchen"}}, "[happy] Kitchen. Going.")
    rocky = Rocky(llm, trips=trips)
    async with aiohttp.ClientSession() as s:
        emotion, words, actions = await rocky.reply(s, "go to the kitchen", spoken=True, may_control=True)
    assert went == [("kitchen", True)] and actions[0]["tool"] == "go_to" and actions[0]["ok"]
    first = llm.calls[0]
    assert any(t["function"]["name"] == "go_to" for t in first["tools"])
    assert "Question: kitchen?" in first["messages"][0]["content"]          # told to ask when unsure


async def test_rocky_cant_be_sent_by_a_stranger():
    trips = types.SimpleNamespace(names=lambda: ["dock", "kitchen"], go=None)
    llm = ScriptedLLM("[neutral] Driver does moving.")
    rocky = Rocky(llm, trips=trips)
    async with aiohttp.ClientSession() as s:
        await rocky.reply(s, "go to the kitchen", "Mallory", may_control=False)
    assert llm.calls[0]["tools"] is None and "You cannot drive yourself" in llm.calls[0]["messages"][0]["content"]


async def test_stop_said_in_the_room_stops_the_ai(tmp_path):
    brain, _ = make_brain(tmp_path)
    brain.rocky, brain.listening = object(), True
    stopped = []
    brain.driver = types.SimpleNamespace(abort=stopped.append)
    brain.driver_task = asyncio.create_task(asyncio.sleep(1))
    await brain.on_heard("Stop! Stop.")
    assert stopped == ["someone in the room said stop"]
    brain.driver_task.cancel()


async def test_rocky_knows_the_trip_he_is_on():
    """Asked mid-trip to the charger, he went by the earlier talk and said he was going to the kitchen."""
    async def go(name, spoken):
        return False, "Already busy: docking. Finish or stop that first."
    trips = types.SimpleNamespace(names=lambda: ["dock", "kitchen"], go=go, now=lambda: "docking")
    llm = ScriptedLLM("[happy] Going home. Charger.")
    rocky = Rocky(llm, trips=trips)
    async with aiohttp.ClientSession() as s:
        await rocky.reply(s, "where are you going", spoken=True, may_control=True)
    assert "You are driving yourself right now: docking" in llm.calls[0]["messages"][0]["content"]

    idle = Rocky(ScriptedLLM("[neutral] Here."), trips=types.SimpleNamespace(names=lambda: ["dock"], go=go,
                                                                               now=lambda: None))
    async with aiohttp.ClientSession() as s:
        await idle.reply(s, "hi", spoken=True, may_control=True)
    assert "driving yourself right now" not in idle.llm.calls[0]["messages"][0]["content"]


async def test_a_second_trip_says_what_the_first_one_is(tmp_path):
    brain, _ = make_brain(tmp_path)
    brain.driver = types.SimpleNamespace(task="going to “kitchen”")
    brain.driver_task = asyncio.create_task(asyncio.sleep(1))
    ok, text = await brain.go_to_place("dock")
    assert not ok and "going to “kitchen”" in text
    assert brain.trips().now() == "going to “kitchen”"
    brain.driver_task.cancel()
