"""Brain: face pipeline on real photos, people store, tracker, greeter, and the full loop
(robot frames -> brain -> faces to driving page -> naming -> greeting spoken on the robot).

Needs OpenCV and the models in $BEAM_BRAIN_MODELS (default ~/beam-brain/models); skipped otherwise.
"""

import asyncio
import base64
import os
import random
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
import numpy as np  # noqa: E402

MODELS = Path(os.environ.get("BEAM_BRAIN_MODELS", Path.home() / "beam-brain" / "models"))
DATA = Path(__file__).parent / "data"
if not (MODELS / "face_recognition_sface_2021dec.onnx").exists():
    pytest.skip(f"face models not found in {MODELS}", allow_module_level=True)
if not ((DATA / "face_a.jpg").exists() and (DATA / "face_b.jpg").exists()):
    pytest.skip("add two photos of different people as tests/data/face_a.jpg and face_b.jpg (see tests/data/README.md)",
                allow_module_level=True)

from beambrain.brain import Brain  # noqa: E402
from beambrain.faces import SAME_PERSON, FaceEngine, similarity  # noqa: E402
from beambrain.greeter import Greeter  # noqa: E402
from beambrain.people import PeopleStore  # noqa: E402
from beambrain.tracker import Tracker  # noqa: E402


@pytest.fixture(scope="module")
def engine():
    return FaceEngine(MODELS)


def photo(name):
    return cv2.imread(str(DATA / name))


def variant(img):
    """The same person in a different shot: mirrored, smaller, darker, shifted."""
    v = cv2.flip(img, 1)
    v = cv2.resize(v, None, fx=0.8, fy=0.8)
    v = cv2.convertScaleAbs(v, alpha=0.8, beta=-10)
    return cv2.copyMakeBorder(v, 30, 0, 40, 0, cv2.BORDER_CONSTANT, value=(40, 40, 40))


def test_finds_one_face_in_each_photo(engine):
    for name in ("face_a.jpg", "face_b.jpg"):
        faces = engine.analyze(photo(name))
        assert len(faces) == 1, name
        assert faces[0].embedding.shape == (128,)
        assert abs(np.linalg.norm(faces[0].embedding) - 1) < 1e-3


def test_same_person_matches_other_person_does_not(engine):
    a = engine.analyze(photo("face_a.jpg"))[0].embedding
    a2 = engine.analyze(variant(photo("face_a.jpg")))[0].embedding
    b = engine.analyze(photo("face_b.jpg"))[0].embedding
    assert similarity(a, a2) > SAME_PERSON
    assert similarity(a, b) < SAME_PERSON


def test_people_store_roundtrip_and_match(engine, tmp_path):
    store = PeopleStore(tmp_path / "people.json")
    f = engine.analyze(photo("face_a.jpg"))[0]
    assert store.add("  Person   A ", [f.embedding], b"\xff\xd8thumb") == "Person A"
    reloaded = PeopleStore(tmp_path / "people.json")
    assert reloaded.match(engine.analyze(variant(photo("face_a.jpg")))[0].embedding)[0] == "Person A"
    assert reloaded.match(engine.analyze(photo("face_b.jpg"))[0].embedding)[0] is None
    assert oct((tmp_path / "people.json").stat().st_mode & 0o777) == "0o600"
    assert reloaded.forget("Person A") and not PeopleStore(tmp_path / "people.json").people


def test_tracker_keeps_identity_and_needs_votes_to_confirm(engine):
    t = Tracker()
    img = photo("face_a.jpg")
    ids = set()
    for i in range(4):
        tracks = t.update(engine.analyze(img))
        ids.add(tracks[0].id)
        tracks[0].votes.append("Messi")
        assert tracks[0].name == ("Messi" if i >= 2 else None)    # confirmed on the 3rd agreeing vote
    assert len(ids) == 1
    for _ in range(8):
        t.update([])
    assert not t.tracks                                          # expires when out of view


def test_greeter_cooldown():
    g = Greeter(cooldown_s=60, rng=random.Random(0))
    assert "Ann" in g.maybe_greet("Ann", now=1000)
    assert g.maybe_greet("Ann", now=1030) is None
    assert g.maybe_greet("Bob", now=1030) is not None
    assert g.maybe_greet("Ann", now=1061) is not None


async def test_full_loop_through_the_server(engine, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from beamctl.server import Config, MotorController, build_app
    from beamctl.sim import SimulatedBoard

    board = SimulatedBoard()
    client = TestClient(TestServer(build_app(MotorController(Config(device=await board.start(), brain_key="k")))))
    await client.start_server()
    brain = Brain(engine, PeopleStore(tmp_path / "people.json"), None, Greeter(cooldown_s=3600))
    stop = asyncio.Event()
    url = str(client.make_url("/ws")) + "?role=brain&key=k"
    runner = asyncio.create_task(brain.run(url, stop))
    try:
        robot = await client.ws_connect("/ws?role=robot")
        pilot = await client.ws_connect("/ws")

        async def next_of(ws, kind, timeout=5.0):
            async def loop():
                while True:
                    m = await ws.receive_json()
                    if m.get("type") == kind:
                        return m
            return await asyncio.wait_for(loop(), timeout)

        async def show(name):
            ok, buf = cv2.imencode(".jpg", photo(name))
            await robot.send_json({"type": "frame", "cam": "head", "w": 0, "h": 0,
                                   "jpeg": base64.b64encode(buf.tobytes()).decode()})

        # Frames get requested once the brain is connected (it may connect before or after the robot).
        frames = await next_of(robot, "frames")
        while frames["fps"] == 0:
            frames = await next_of(robot, "frames")
        assert frames["fps"] > 0

        # An unknown face shows up on the driving page without a name.
        await show("face_a.jpg")
        faces = (await next_of(pilot, "faces"))["faces"]
        assert len(faces) == 1 and faces[0]["name"] is None

        # The driver names it; the brain confirms and lists the person.
        await pilot.send_json({"type": "brain", "data": {"kind": "enroll", "face_id": faces[0]["id"], "name": "Leo"}})
        assert "Leo" in (await next_of(pilot, "notice"))["text"]
        people = (await next_of(pilot, "people"))["people"]
        assert [p["name"] for p in people] == ["Leo"] and people[0]["thumb"]

        # Next frames: recognised by name. (No greeting right after being introduced.)
        await asyncio.sleep(0.2)
        await show("face_a.jpg")
        faces = (await next_of(pilot, "faces"))["faces"]
        assert faces[0]["name"] == "Leo"
        # The robot screen is told who it's looking at.
        seen = await next_of(robot, "seen")
        while seen["names"] != ["Leo"]:
            seen = await next_of(robot, "seen")

        # Someone else stays unknown.
        brain.tracker = Tracker()
        await asyncio.sleep(0.2)
        await show("face_b.jpg")
        assert (await next_of(pilot, "faces"))["faces"][0]["name"] is None

        # After the cooldown, Leo is greeted out loud on the robot.
        brain.greeter.last.clear()
        brain.tracker = Tracker()
        for _ in range(4):
            await asyncio.sleep(0.2)
            await show("face_a.jpg")
        say = await next_of(robot, "say", timeout=8)
        assert "Leo" in say["text"]
        await robot.close()
        await pilot.close()
    finally:
        stop.set()
        await runner
        await client.close()
        await board.stop()
