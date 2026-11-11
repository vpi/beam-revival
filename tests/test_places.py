"""Named places on the map: saving them where the robot stands, going there from the driving page or by asking
Rocky, and "stop" said in the room."""

import asyncio
import types

import aiohttp

from beambrain.brain import Brain
from beambrain.greeter import Greeter
from beambrain.people import PeopleStore
from beambrain.places import DOCK, Places, clean_names
from beambrain.rocky import Rocky, yes_or_no
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


async def test_rocky_asks_before_a_trip_asked_in_the_room():
    """Seen at home: a wave woke him mid-conversation, and chatter not meant for him started a trip."""
    went, asked = [], []

    async def go(name, spoken):
        went.append((name, spoken))
        return True, "Started going to “kitchen”."

    async def ask(name):
        asked.append(name)
        return True, "kitchen"
    trips = types.SimpleNamespace(names=lambda: ["dock", "charger", "kitchen"], go=go, ask=ask)
    llm = ScriptedLLM({"name": "go_to", "arguments": {"place": "kitchen"}}, "[happy] Kitchen. Going.")
    rocky = Rocky(llm, trips=trips)
    async with aiohttp.ClientSession() as s:
        emotion, words, actions = await rocky.reply(s, "go to the kitchen", spoken=True, may_control=True)
    assert went == [] and asked == ["kitchen"]                              # not moving yet
    assert words == "Going to kitchen. OK?" and actions[0]["asked"]
    first = llm.calls[0]
    assert any(t["function"]["name"] == "go_to" for t in first["tools"])
    assert "Question: kitchen?" in first["messages"][0]["content"]          # told to ask when unsure
    assert len(llm.calls) == 1                                              # fixed words, not the AI's

    typed = Rocky(ScriptedLLM({"name": "go_to", "arguments": {"place": "kitchen"}}, "[happy] Going."), trips=trips)
    async with aiohttp.ClientSession() as s:                                # a driver typing: at once
        await typed.reply(s, "go to the kitchen", "Ann", spoken=False, may_control=True)
    assert went == [("kitchen", False)]


def test_yes_or_no():
    for yes in ("Yes.", "Okay!", "yeah go ahead", "Yes please", "Do it.", "OK, Rocky", "go", "Sure."):
        assert yes_or_no(yes) == "yes", yes
    for no in ("No.", "No, stay", "don't go", "wait", "Cancel that", "not now", "okay no"):
        assert yes_or_no(no) == "no", no
    for other in ("", "Thank you.", "please", "right", "yes I told you he was going to the kitchen later",
                  "it", "and then we went"):
        assert yes_or_no(other) is None, other


async def heard_brain(tmp_path):
    brain, sent = make_brain(tmp_path)
    brain.rocky, brain.listening = object(), True
    brain.localizer = types.SimpleNamespace(pose=(1.5, -0.5, 1.0), state="here")
    kitchen = brain.places.add("kitchen", (3.0, 1.0, 0.0))
    trips, said, chats = [], [], []

    async def run_move(kind, amount=0.0, place=None):
        trips.append((kind, place and place["id"]))
        return None

    async def rocky_say(words, emotion):
        said.append(words)
        return None, None

    async def start_chat(text, sender, spoken=False, login=None):
        chats.append(text)
    brain.run_move, brain.rocky_say, brain.start_chat = run_move, rocky_say, start_chat
    return brain, kitchen, trips, said, chats


async def test_a_trip_asked_in_the_room_starts_only_on_yes(tmp_path):
    brain, kitchen, trips, said, chats = await heard_brain(tmp_path)
    assert await brain.trips().ask("kitchen") == (True, "kitchen") and trips == []
    await brain.on_heard("Yes please.")
    await asyncio.sleep(0.01)
    assert trips == [("goto", kitchen["id"])] and said == ["Yes yes. Going."] and brain.pending_trip is None


async def test_no_or_anything_else_means_no_trip(tmp_path):
    brain, kitchen, trips, said, chats = await heard_brain(tmp_path)
    await brain.ask_trip("kitchen")
    await brain.on_heard("No, stay.")
    await asyncio.sleep(0.01)
    assert trips == [] and said == ["Okay. Stay here."]
    await brain.on_heard("yes")                                             # a late yes: nothing pending
    assert trips == []

    await brain.ask_trip("kitchen")
    brain.wake.wake_up()
    await brain.on_heard("and then he said the kitchen was a mess")         # chatter: just talk
    await asyncio.sleep(0.01)
    assert trips == [] and brain.pending_trip is None and chats[-1] == "and then he said the kitchen was a mess"

    await brain.ask_trip("kitchen")
    brain.pending_trip = (brain.pending_trip[0], 0.0)                       # 20 s went by
    await brain.on_heard("Yes.")
    assert trips == []

    await brain.ask_trip("kitchen")
    await brain.on_listen(True)
    brain.listening = True
    await brain.on_listen(False)                                            # someone took the controls
    assert brain.pending_trip is None


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
    assert "You are driving yourself right now: going back to your charger" in llm.calls[0]["messages"][0]["content"]

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


async def test_mid_trip_he_names_only_this_trip():
    """On the way to the dock after talk of the TV, he said "dock, then living room": he can't line trips up."""
    trips = types.SimpleNamespace(names=lambda: ["dock", "tv"], go=None, now=lambda: "docking")
    llm = ScriptedLLM("[thinking] Charger. Going home.")
    rocky = Rocky(llm, trips=trips)
    async with aiohttp.ClientSession() as s:
        await rocky.reply(s, "where are you going", spoken=True, may_control=True)
    system = llm.calls[0]["messages"][0]["content"]
    assert "going back to your charger" in system and "none is planned" in system
    assert "can't line up trips" in system


async def test_the_log_says_if_a_place_name_was_heard_never_the_words(tmp_path, caplog):
    import logging
    brain, _ = make_brain(tmp_path)
    brain.places.add("watch tv, tv", (1.0, 0.0, 0.0))

    class Says:
        async def reply(self, *a):
            return "happy", "Is good.", []
    brain.rocky = Says()

    async def say(words, emotion):
        return None, None
    brain.rocky_say = say
    with caplog.at_level(logging.INFO, logger="beambrain"):
        await brain._chat("go watch TV please", None, spoken=True)
        await brain._chat("what's the weather", None, spoken=True)
    lines = [r.getMessage() for r in caplog.records if "Rocky answered" in r.getMessage()]
    assert lines[0].endswith("; a place name in what was asked")
    assert lines[1].endswith("; no place name in what was asked")
    assert not any("watch" in r.getMessage().lower() or "weather" in r.getMessage() for r in caplog.records)
