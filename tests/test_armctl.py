"""The motor server with the arm (ArmController in beamctl/arm.py), against the simulated arm: moves, the freeze,
the E-stop, sleep, the interlock with the wheels, and who may move it. In none of them is {"T":0} ever sent."""

import asyncio
import json
import math
import os
import time
import types

import pytest

from beamctl import arm as armmod
from beamctl.armsim import RADIO_MISSION, SimulatedArm
from beamctl.lidar import Lidar
from beamctl.server import Config, MotorController

STOW_MISSION = RADIO_MISSION + [{"T": 121, "joint": 3, "angle": 180, "spd": 30, "acc": 10}, {"T": 111, "cmd": 4000},
                                {"T": 121, "joint": 1, "angle": 93, "spd": 20, "acc": 10}, {"T": 111, "cmd": 5500},
                                {"T": 121, "joint": 2, "angle": -90, "spd": 20, "acc": 10}]


async def until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.fixture
async def rig(monkeypatch):
    monkeypatch.setattr(armmod, "RETRY_S", 0.1)
    monkeypatch.setattr(armmod, "LISTEN_S", 0.2)
    made = []

    async def make(mission=STOW_MISSION, move_scale=20, **cfg):
        sim = SimulatedArm(None, mission=mission, boot_s=0.05, time_scale=0.01, move_scale=move_scale)
        await sim.start()
        c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None, matrix_device=None,
                                   arm_device=sim.path, reopen_interval_s=0.05, **cfg))
        c.start()
        made.append((sim, c))
        assert await until(lambda: c.arm.arm.state == "ready", 15)
        await until(lambda: c.arm.joints_deg() is not None, 2)
        return sim, c
    yield make
    for sim, c in made:
        await c.stop()
        assert sim.t0_received == 0                           # never, in any test
        await sim.stop()


def driving(c, lin=0.3, ang=0.0, who=None):
    who = who or object()
    c.set_target(lin, ang, who)
    return c._effective_target(time.monotonic())


def test_no_arm_by_default():
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None, matrix_device=None))
    assert c.arm is None and c.snapshot()["arm"] is None
    assert driving(c)[0] > 0


def test_an_arm_not_named_is_left_out_not_a_crash():
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None, matrix_device=None,
                               arm_device="auto"))
    assert c.arm is None


async def test_it_drives_only_with_the_arm_stowed(rig):
    """Booted with only the radio steps, the arm is in its start pose: out. No driving until it's stowed."""
    sim, c = await rig(mission=RADIO_MISSION)
    assert c.arm.state() == "holding" and c.snapshot()["arm"]["drive"] == "out"
    me = object()
    assert driving(c, who=me) == (0.0, 0.0)
    ok, why = c.arm_command({"type": "arm_stow"}, me)          # the driver has the arm too
    assert ok, why
    assert await until(lambda: c.arm.state() == "stowed", 10)
    assert c.arm.joints_deg()["shoulder"] == pytest.approx(-90, abs=4)
    assert driving(c, who=me)[0] > 0


async def test_while_its_pose_isnt_known_it_drives_slowly(rig):
    sim, c = await rig()
    assert await until(lambda: c.arm.state() == "stowed", 5)
    sim.silent = True                                          # stops answering
    assert await until(lambda: c.arm.drive_limit() == "unknown", 5)
    lin, ang = driving(c, 0.5, 1.0)
    assert lin == c.cfg.obstacle_blind_speed and ang == c.cfg.arm_unknown_angular


async def test_stop_freezes_it_mid_move(rig):
    sim, c = await rig(mission=RADIO_MISSION, move_scale=1)
    me = object()
    assert c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 150}, me)[0]
    await until(lambda: math.degrees(sim.pos["e"]) > 100, 5)
    ok, _ = c.arm_command({"type": "arm_stop"}, object())      # anyone may stop it
    assert ok and not c.arm.moving
    held = math.degrees(sim.pos["e"])
    await asyncio.sleep(0.5)
    assert math.degrees(sim.pos["e"]) == pytest.approx(held, abs=1.5) and held < 140
    assert c.arm.state() == "holding" and driving(c) == (0.0, 0.0)


async def test_the_estop_freezes_it_and_refuses_orders_until_released(rig):
    sim, c = await rig(mission=RADIO_MISSION, move_scale=1)
    me = object()
    c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 150}, me)
    await until(lambda: math.degrees(sim.pos["e"]) > 100, 5)
    c.set_estop(True, me)
    assert not c.arm.moving
    ok, why = c.arm_command({"type": "arm_stow"}, me)
    assert not ok and "STOP" in why
    c.set_estop(False, me)
    assert c.arm_command({"type": "arm_stow"}, me)[0]


async def test_sleep_stows_then_lets_go_and_waking_doesnt_move_it(rig):
    sim, c = await rig(mission=RADIO_MISSION)
    c.go_to_sleep("test")
    assert await until(lambda: not any(sim.torque.values()), 15)
    assert c.arm.stowed()
    ok, why = c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 120}, object())
    assert not ok and "asleep" in why
    before = dict(sim.pos)
    c.wake_up("test")
    assert await until(lambda: all(sim.torque.values()), 5)
    await asyncio.sleep(0.3)
    assert sim.pos == pytest.approx(before, abs=1e-3)


async def test_the_arm_wont_leave_the_stow_while_the_wheels_turn(rig):
    sim, c = await rig()
    assert await until(lambda: c.arm.state() == "stowed", 5)
    c.link.commanded_linear = 0.2
    ok, why = c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 150}, object())
    assert not ok and "moving" in why
    c.link.commanded_linear = 0.0
    assert c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 150}, object())[0]


async def test_a_move_stops_when_its_driver_leaves(rig):
    sim, c = await rig(mission=RADIO_MISSION, move_scale=1)
    me = object()
    c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 150}, me)
    await until(lambda: math.degrees(sim.pos["e"]) > 100, 5)
    c.release(me)
    assert not c.arm.moving and c.arm.why == "its driver left"


async def test_whoever_drives_has_the_arm(rig):
    sim, c = await rig()
    driver, other = object(), object()
    c.set_target(0.0, 0.0, driver)                             # driving (standing still) right now
    ok, why = c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 150}, other)
    assert not ok and "another driver" in why
    assert c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 150}, driver)[0]


async def test_joints_stay_in_their_boxes(rig):
    sim, c = await rig()
    me = object()
    for joint, deg in (("base", 180), ("shoulder", 60), ("elbow", 10), ("gripper", 300), ("wrist", 0)):
        ok, _ = c.arm_command({"type": "arm_joint", "joint": joint, "deg": deg}, me)
        assert not ok, joint
    assert not c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": float("nan")}, me)[0]


async def test_a_move_that_meets_something_freezes(rig, monkeypatch):
    sim, c = await rig(mission=RADIO_MISSION, move_scale=1)
    monkeypatch.setattr(armmod, "LOAD_JUMP", 5)              # the sim's load goes 35 -> 50 when it moves
    assert c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 150}, object())[0]
    assert await until(lambda: not c.arm.moving, 3)
    assert "met something" in c.arm.why


async def test_the_status_says_what_the_arm_does(rig):
    sim, c = await rig()
    a = c.snapshot()["arm"]
    assert a["present"] and a["state"] == "stowed" and a["drive"] is None and a["volts"] == 12.32
    assert set(a["joints"]) == {"base", "shoulder", "elbow", "gripper"}
    c.arm_command({"type": "arm_joint", "joint": "elbow", "deg": 150}, object())
    assert c.snapshot()["arm"]["by"] == "human"


def test_looking_for_the_lidar_never_opens_the_arms_port(tmp_path):
    real = tmp_path / "ttyUSB2"
    real.write_text("")
    link = tmp_path / "usb-arm"
    os.symlink(real, link)
    lid = Lidar(str(real), claimed={str(link)})                # the same port, by another name
    assert lid._candidates() == []
    assert Lidar(str(real))._candidates() == [str(real)]


async def test_a_held_button_moves_a_joint_and_letting_go_freezes_it(rig):
    sim, c = await rig(mission=RADIO_MISSION, move_scale=1)
    me = object()
    start = math.degrees(sim.pos["e"])
    for _ in range(6):                                        # held for about a second
        ok, why = c.arm_command({"type": "arm_jog", "joint": "elbow", "dir": 1}, me)
        assert ok, why
        await asyncio.sleep(0.2)
    moved = math.degrees(sim.pos["e"]) - start
    assert 12 < moved < 30 and c.snapshot()["arm"]["by"] == "human"
    await asyncio.sleep(armmod.JOG_DEADMAN_S + 0.4)            # let go: no more jogs
    assert not c.arm.moving
    held = math.degrees(sim.pos["e"])
    await asyncio.sleep(0.5)
    assert math.degrees(sim.pos["e"]) == pytest.approx(held, abs=1.5)


async def test_jogging_keeps_to_the_joint_box_and_the_interlock(rig):
    sim, c = await rig()
    assert await until(lambda: c.arm.state() == "stowed", 5)
    me = object()
    ok, why = c.arm_command({"type": "arm_jog", "joint": "elbow", "dir": 1}, me)     # elbow at 180: the top
    assert not ok and "limit" in why
    c.link.commanded_linear = 0.2                                                      # the wheels turning
    ok, why = c.arm_command({"type": "arm_jog", "joint": "elbow", "dir": -1}, me)
    assert not ok and "moving" in why
    c.link.commanded_linear = 0.0
    assert not c.arm_command({"type": "arm_jog", "joint": "elbow", "dir": 2}, me)[0]
    assert not c.arm_command({"type": "arm_jog", "joint": "wrist", "dir": 1}, me)[0]


async def test_the_light_goes_off_when_its_driver_leaves_or_the_robot_sleeps(rig):
    sim, c = await rig()
    me = object()
    ok, why = c.arm_command({"type": "arm_light", "level": 255}, me)
    assert ok and why == "light 100%"
    assert await until(lambda: sim.led == 255, 2) and c.snapshot()["arm"]["light"] == 255
    c.release(me)                                              # the page closed
    assert await until(lambda: sim.led == 0, 2) and c.snapshot()["arm"]["light"] == 0
    c.arm_command({"type": "arm_light", "level": 128}, me)
    assert await until(lambda: sim.led == 128, 2)
    c.go_to_sleep("test")
    assert await until(lambda: sim.led == 0, 10)
    assert await until(lambda: c.arm.asleep, 10)
    ok, why = c.arm_command({"type": "arm_light", "level": 255}, me)
    assert not ok and "asleep" in why
    c.wake_up("test")
    assert await until(lambda: not c.arm.asleep, 5)
    for bad in (None, "x", float("nan")):
        assert not c.arm_command({"type": "arm_light", "level": bad}, me)[0], bad
    assert c.arm_command({"type": "arm_light", "level": 9999}, me)[0] and await until(lambda: sim.led == 255, 2)


async def test_the_stow_closes_the_gripper_first(rig):
    sim, c = await rig(mission=RADIO_MISSION)
    me = object()
    assert c.arm_command({"type": "arm_joint", "joint": "gripper", "deg": 100}, me)[0]      # left open
    assert await until(lambda: not c.arm.moving and c.arm.joints_deg()["gripper"] < 105, 5)
    before = len(sim.received)
    ok, why = c.arm_command({"type": "arm_stow"}, me)
    assert ok and why == "stowing the arm"
    assert await until(lambda: c.arm.state() == "stowed", 15)
    moves = [m["joint"] for m in sim.received[before:] if m.get("T") == 121]
    assert moves == [4, 2, 3, 1]                                  # gripper, shoulder, elbow, base
    assert c.arm.joints_deg()["gripper"] == pytest.approx(180, abs=4)


async def test_stowed_with_the_gripper_open_only_closes_it(rig):
    sim, c = await rig()
    assert await until(lambda: c.arm.state() == "stowed", 5)
    me = object()
    c.arm_command({"type": "arm_joint", "joint": "gripper", "deg": 100}, me)
    assert await until(lambda: not c.arm.moving, 5)
    assert c.arm.state() == "stowed"                              # an open gripper still drives
    before = len(sim.received)
    ok, why = c.arm_command({"type": "arm_stow"}, me)
    assert ok and why == "closing the gripper"
    assert await until(lambda: not c.arm.moving, 5)
    assert [m["joint"] for m in sim.received[before:] if m.get("T") == 121] == [4]
    assert c.arm_command({"type": "arm_stow"}, me) == (True, "the arm is stowed")


async def test_a_gesture_starts_and_ends_stowed(rig):
    sim, c = await rig()
    assert await until(lambda: c.arm.state() == "stowed", 5)
    before = len(sim.received)
    ok, why = c.arm_command({"type": "arm_gesture", "name": "wave"}, object())
    assert ok, why
    assert await until(lambda: c.arm.moving, 2)
    assert await until(lambda: not c.arm.moving, 20)
    assert c.arm.state() == "stowed" and c.arm.why is None
    elbow = [m["angle"] for m in sim.received[before:] if m.get("T") == 121 and m["joint"] == 3]
    assert elbow[:4] == [140, 165, 140, 165] and elbow[-1] == 180
    assert not c.arm_command({"type": "arm_gesture", "name": "backflip"}, object())[0]


async def test_gestures_start_only_from_the_stow(rig):
    sim, c = await rig(mission=RADIO_MISSION)
    ok, why = c.arm_command({"type": "arm_gesture", "name": "wave"}, object())
    assert not ok and "stow" in why


async def test_the_ai_waits_for_people_and_any_person_stops_it(rig, monkeypatch):
    from beamctl import server
    sim, c = await rig(move_scale=1)
    assert await until(lambda: c.arm.state() == "stowed", 5)
    brain, person = object(), object()
    c.set_target(0.0, 0.0, person)                              # a person just touched the controls
    ok, why = c.arm_command({"type": "arm_gesture", "name": "wave"}, brain, server.PRIORITY_BRAIN)
    assert not ok and "person" in why
    monkeypatch.setattr(server, "BRAIN_ARM_QUIET_S", 0.0)
    c.drive.last_input = -1e9                                   # their dead-man long gone
    ok, why = c.arm_command({"type": "arm_gesture", "name": "wave"}, brain, server.PRIORITY_BRAIN)
    assert ok, why
    assert c.snapshot()["arm"]["by"] == "brain"
    await until(lambda: math.degrees(sim.pos["e"]) < 170, 5)
    c.set_target(0.0, 0.0, person)                              # the person drives: the AI's move stops
    assert not c.arm.moving and c.arm.why == "a person took over"


async def test_a_load_that_flips_sign_when_a_joint_reverses_isnt_a_collision(rig):
    """On the real arm a wave froze at its first turn: the servos report load signed, and it went from about
    -150 to +150 as the elbow reversed. Only a bigger load counts, whichever way."""
    sim, c = await rig()
    assert await until(lambda: c.arm.state() == "stowed", 5)
    flip = {"n": 0}
    real = sim._feedback

    def flipping():                                  # the elbow's load swings between -150 and +150
        flip["n"] += 1
        return real().replace('"torE":', '"torE":' + ("-" if flip["n"] % 2 else ""), 1) \
            .replace('"torE":-35', '"torE":-150').replace('"torE":-50', '"torE":-150') \
            .replace('"torE":35', '"torE":150').replace('"torE":50', '"torE":150')
    sim._feedback = flipping
    ok, _ = c.arm_command({"type": "arm_gesture", "name": "wave"}, object())
    assert ok and await until(lambda: not c.arm.moving, 20)
    assert c.arm.why is None and c.arm.state() == "stowed"


async def hold_button(c, me, joint, direction, seconds):
    """A page holding − or + for a while, then letting go (the page sends arm_stop at once)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ok, why = c.arm_command({"type": "arm_jog", "joint": joint, "dir": direction}, me)
        assert ok, why
        await asyncio.sleep(0.2)
    c.arm_command({"type": "arm_stop"}, me)
    await asyncio.sleep(0.3)


async def test_a_gesture_taught_from_the_page_is_kept_and_replayed_in_order(rig, tmp_path):
    sim, c = await rig(move_scale=1, sleep_state=str(tmp_path / "sleep.json"))
    assert await until(lambda: c.arm.state() == "stowed", 5)
    me = object()
    ok, why = c.arm_command({"type": "arm_record", "on": True}, me)
    assert ok, why
    await hold_button(c, me, "elbow", -1, 0.8)                  # elbow out a bit
    await hold_button(c, me, "elbow", -1, 0.4)                  # ...and a bit more: still one move
    await hold_button(c, me, "base", -1, 0.6)                   # then the base
    rec = c.snapshot()["arm"]["recording"]
    assert rec == 2
    elbow_at = c.arm.recording[0][1]
    assert c.arm.recording[0][0] == "elbow" and c.arm.recording[1][0] == "base" and elbow_at < 170
    for bad in ("wave", "", "!!", "fist_bump"):
        assert not c.arm_command({"type": "arm_save", "name": bad}, me)[0], bad
    ok, why = c.arm_command({"type": "arm_save", "name": "  High  Five "}, me)
    assert ok, why
    snap = c.snapshot()["arm"]
    assert snap["recording"] is None and "high five" in snap["gestures"] and snap["taught"] == ["high five"]
    saved = json.loads((tmp_path / "arm-gestures.json").read_text())
    assert [m[0] for m in saved["gestures"]["high five"]] == ["elbow", "base"]
    # Kept across a restart (a new controller reads the file).
    from beamctl.arm import ArmController
    again = ArmController(c.arm.arm, taught_file=str(tmp_path / "arm-gestures.json"))
    assert again.taught["high five"][0][:2] == ("elbow", elbow_at)
    # Replayed from the stow, in the taught order, then the stow.
    assert c.arm_command({"type": "arm_stow"}, me)[0]
    assert await until(lambda: c.arm.state() == "stowed", 20)
    before = len(sim.received)
    assert c.arm_command({"type": "arm_gesture", "name": "high five"}, me)[0]
    assert await until(lambda: c.arm.moving, 2) and await until(lambda: not c.arm.moving, 30)
    moves = [(m["joint"], m["angle"]) for m in sim.received[before:] if m.get("T") == 121]
    assert moves[0] == (3, elbow_at) and moves[1][0] == 1 and moves[2:] == [(3, 180.0), (1, 93.0), (2, -90.0)]
    assert c.arm.state() == "stowed"
    assert c.arm_command({"type": "arm_forget", "name": "high five"}, me)[0]
    assert "high five" not in c.snapshot()["arm"]["gestures"]
    assert json.loads((tmp_path / "arm-gestures.json").read_text())["gestures"] == {}


async def test_teaching_starts_from_the_stow_and_ends_when_its_page_leaves(rig):
    sim, c = await rig(mission=RADIO_MISSION)
    me = object()
    ok, why = c.arm_command({"type": "arm_record", "on": True}, me)
    assert not ok and "stow" in why
    assert c.arm_command({"type": "arm_stow"}, me)[0]
    assert await until(lambda: c.arm.state() == "stowed", 20)
    assert c.arm_command({"type": "arm_record", "on": True}, me)[0]
    ok, why = c.arm_command({"type": "arm_gesture", "name": "wave"}, me)
    assert not ok and "taught" in why                            # no playing while teaching
    c.release(me)
    assert c.arm.recording is None


def test_bad_taught_gestures_in_the_file_are_left_out(tmp_path):
    from beamctl.arm import ArmController
    f = tmp_path / "arm-gestures.json"
    f.write_text(json.dumps({"gestures": {"ok one": [["elbow", 150]], "wave": [["elbow", 150]],
                                          "too far": [["elbow", 10]], "bad joint": [["wrist", 1]],
                                          "Bad Name!": [["elbow", 150]], "empty": []}}))
    a = ArmController(types.SimpleNamespace(), taught_file=str(f))
    assert list(a.taught) == ["ok one"]
    f.write_text("{not json")
    assert ArmController(types.SimpleNamespace(), taught_file=str(f)).taught == {}
