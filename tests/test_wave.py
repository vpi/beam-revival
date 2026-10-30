"""Rocky step 4: a wave beside a face wakes him; other movement doesn't."""

import asyncio

import numpy as np
import pytest

from beambrain.wave import COOLDOWN_S, WaveDetector

FACE = (280, 150, 80, 80)


def frame(hand_x=None, light=120):
    img = np.full((480, 640, 3), light, np.uint8)
    img[150:230, 280:360] = 200                          # the face
    if hand_x is not None:
        img[90:180, hand_x:hand_x + 60] = 240              # a raised hand, left of the face
    return img


def run(detector, frames, faces=(FACE,), start=0.0):
    return [detector.update(img, list(faces), start + i / 3) for i, img in enumerate(frames)]


def test_a_wave_wakes_him_once():
    d = WaveDetector()
    waving = [frame(120 if i % 2 else 190) for i in range(12)]
    woke = run(d, waving)
    assert woke.index(True) <= 5 and woke.count(True) == 1       # once, then the cooldown
    assert run(d, waving, start=COOLDOWN_S + 10).count(True) == 1


def test_no_face_no_wave():
    assert not any(run(WaveDetector(), [frame(120 if i % 2 else 190) for i in range(12)], faces=()))


def test_a_still_person_is_not_a_wave():
    assert not any(run(WaveDetector(), [frame(120) for _ in range(12)]))


def test_light_or_camera_changes_are_not_a_wave():
    flicker = [frame(120 if i % 2 else 190, light=60 if i % 2 else 180) for i in range(12)]
    assert not any(run(WaveDetector(), flicker))


def test_a_hand_hiding_the_face_still_counts():
    d = WaveDetector()
    woke = [d.update(frame(120 if i % 2 else 190), [FACE] if i % 3 == 0 else [], i / 3) for i in range(8)]
    assert any(woke)


def test_walking_up_to_him_is_not_a_wave():
    d = WaveDetector()
    walking = [(frame(120 if i % 2 else 190), (280 - 15 * i, 150, 80 + 20 * i, 80 + 20 * i)) for i in range(10)]
    assert not any(d.update(img, [box], i / 3) for i, (img, box) in enumerate(walking))


def test_moving_close_with_the_face_hidden_is_not_a_wave():
    # From the robot: a step (the face jumps), then the face lost while a lot moves beside where it was.
    d = WaveDetector()
    faces = [[FACE], [FACE], [], [(200, 150, 160, 160)], [], [], []]
    assert not any(d.update(frame(120 if i % 2 else 190), f, i / 3) for i, f in enumerate(faces))


def test_frames_too_far_apart_are_not_compared():
    d = WaveDetector()
    assert not any(d.update(frame(120 if i % 2 else 190), [FACE], i * 2.0) for i in range(12))


async def test_brain_wakes_on_a_wave():
    pytest.importorskip("cv2")
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore

    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    sent, said = [], []
    brain.send = lambda m: asyncio.sleep(0, sent.append(m))
    brain.rocky_say = lambda words, emotion: asyncio.sleep(0, said.append(words))
    await brain.on_wave()
    assert brain.wake.awake
    assert {"type": "rocky_awake", "seconds": brain.wake.awake_s, "by": "wave"} in sent
    await brain.wake_reply
    assert said == ["Question?"]
