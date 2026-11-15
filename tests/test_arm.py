"""The robot arm's protocol and reader (beamctl/arm.py), against the simulated arm (beamctl/armsim.py).

What the real arm taught us is the point of most of these: its boot mission (radio off, the stow) is aborted by
any byte that arrives while it plays, so the reader must stay silent while the arm starts; and {"T":0} must never
be sent.
"""

import asyncio
import json
import math
import os
import time

import pytest

from beamctl import arm as armmod
from beamctl.arm import Arm, Feedback, Lines, line
from beamctl.armsim import RADIO_MISSION, SimulatedArm

STOW = RADIO_MISSION + [{"T": 121, "joint": 3, "angle": 180, "spd": 30, "acc": 10}, {"T": 111, "cmd": 4000},
                        {"T": 121, "joint": 1, "angle": 94, "spd": 20, "acc": 10}, {"T": 111, "cmd": 5500},
                        {"T": 121, "joint": 2, "angle": -90, "spd": 20, "acc": 10}]


# --- the protocol ---

def test_commands_as_the_firmware_reads_them():
    assert armmod.read_state() == b'{"T":105}\n'
    assert armmod.joints(0, 0, 1.5708, 3.14, spd=0, acc=10) == \
        b'{"T":102,"base":0,"shoulder":0,"elbow":1.5708,"hand":3.14,"spd":0,"acc":10}\n'
    assert armmod.joint_deg(3, 180, 30, 10) == b'{"T":121,"joint":3,"angle":180,"spd":30,"acc":10}\n'
    assert armmod.grip(9) == b'{"T":106,"cmd":3.14,"spd":0,"acc":0}\n'           # clamped: closed
    assert armmod.grip_force(5000) == b'{"T":107,"tor":1000}\n'
    assert armmod.torque_limits(60, 110, 50, 50) == b'{"T":112,"mode":1,"b":60,"s":110,"e":50,"h":50}\n'
    assert armmod.torque(False) == b'{"T":210,"cmd":0}\n' and armmod.quiet() == b'{"T":605,"cmd":0}\n'
    assert armmod.radio_off() == [b'{"T":301,"mode":0}\n', b'{"T":300,"mode":0,"mac":"00:00:00:00:00:00"}\n']
    with pytest.raises(ValueError):
        armmod.joint_deg(5, 0)


def test_the_emergency_stop_that_isnt_one_is_never_built():
    """{"T":0} drops the arm and makes it deaf for 10 s."""
    with pytest.raises(ValueError):
        line({"T": 0})


def test_lines_skip_text_and_wait_for_the_rest():
    ls = Lines()
    objs, texts = ls.feed(b'Power up the servos.\n{"T":1051,"b":0.1,')
    assert objs == [] and texts == ["Power up the servos."]
    objs, texts = ls.feed(b'"s":0}\n{bad json\n\n[missionPlay finished.]\r\n')
    assert objs == [{"T": 1051, "b": 0.1, "s": 0}] and texts == ["{bad json", "[missionPlay finished.]"]
    ls.feed(b"x" * 5000)                                         # no newline in a long while: dropped
    assert ls.buf == b""


def test_feedback():
    d = {"T": 1051, "b": 1.6, "s": -1.57, "e": 3.13, "t": 3.14, "torB": 68, "torS": 64, "torE": -36, "torH": 24,
         "torswitchB": 1, "torswitchS": 1, "torswitchE": 1, "torswitchH": 0, "v": 1232}
    fb = Feedback.parse(d, 5.0)
    assert fb.loads == (68, 64, -36, 24) and fb.torque == (True, True, True, False) and fb.volts == 12.32
    assert fb.degrees() == {"b": 91.7, "s": -90.0, "e": 179.3, "t": 179.9}
    assert Feedback.parse({"T": 1051, "b": "x"}, 0) is None and Feedback.parse({"T": 1051, "b": 0}, 0) is None
    assert Feedback.parse({**d, "e": float("nan")}, 0) is None
    old = Feedback.parse({k: v for k, v in d.items() if not k.startswith("torswitch") and k != "v"}, 0)
    assert old.torque is None and old.volts is None                     # older firmware: no switches, no volts


def test_it_runs_only_on_a_named_port():
    for device in ("", "auto", None):
        with pytest.raises(ValueError):
            Arm(device)
    claimed = set()
    Arm("/dev/serial/by-id/usb-the-arm", claimed)
    assert claimed == {"/dev/serial/by-id/usb-the-arm"}               # claimed before any sensor looks


# --- the reader, on the simulated arm ---

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
    monkeypatch.setattr(armmod, "LISTEN_S", 0.3)
    made = []

    async def make(mission=None, boot=True, grace_s=5.0, start_arm=True, **kw):
        sim = SimulatedArm(None, mission=mission, boot_s=0.1, time_scale=0.01, move_scale=20, **kw)
        await sim.start(boot=boot)
        a = Arm(sim.path, grace_s=grace_s)
        made.append((sim, a))
        if start_arm:
            a.start()
        return sim, a
    yield make
    for sim, a in made:
        await a.stop()
        await sim.stop()


def sent_types(a):
    return [json.loads(d)["T"] for d in a.sent]


async def test_it_waits_out_the_boot_mission_then_turns_the_radio_off(rig):
    """Plugged in (USB power is the arm's power): the port is new, so not a byte until the mission says it's
    done. Then it checks it's the arm, turns debug text and radio control off, and polls."""
    sim, a = await rig(mission=STOW)
    assert a.state == "booting" or await until(lambda: a.state == "booting", 1)
    assert await until(lambda: a.state == "ready", 10)
    assert sim.mission_done and not sim.aborted
    assert sim.pos["e"] == pytest.approx(math.pi, abs=0.01) and sim.pos["s"] == pytest.approx(-math.pi / 2, abs=0.01)
    assert sent_types(a)[:4] == [105, 605, 301, 300]
    assert sim.espnow_mode == 0 and sim.info_print == 0
    assert await until(lambda: a.fresh and a.snapshot()["joints"]["b"] == pytest.approx(94, abs=0.5), 2)
    assert a.snapshot()["volts"] == 12.32 and sim.t0_received == 0


async def test_one_byte_during_the_mission_would_have_aborted_it(rig):
    """What the reader avoids: the sim, like the firmware, gives up the rest of the mission on any input."""
    sim, _ = await rig(mission=STOW, start_arm=False)
    await until(lambda: sim._in_mission, 5)
    os.write(os.open(sim.path, os.O_WRONLY | os.O_NOCTTY), b'{"T":105}\n')
    assert await until(lambda: not sim._in_mission, 5)
    assert sim.aborted and not sim.mission_done
    assert sim.pos["s"] == pytest.approx(0, abs=0.01)                # never stowed


async def test_an_arm_already_running_is_listened_to_first_then_used(rig, monkeypatch):
    """The server restarting with the arm long since started: listen a moment (in case it's starting after all),
    then use it at once."""
    sim, a = await rig(start_arm=False, grace_s=0.5)
    await until(lambda: sim.mission_done, 5)
    await asyncio.sleep(0.6)                                          # the port is older than the grace now
    a.start()
    assert await until(lambda: a.state == "ready", 5)
    assert a.boots == 0 and not sim.aborted


async def test_a_restart_while_polled_is_waited_out_too(rig):
    """{"T":600} or its power cycled with the USB kept: it prints its boot text again; the reader goes quiet
    until the mission is done, and the radio goes off again (ESP-NOW comes back on at every start)."""
    sim, a = await rig(mission=STOW)
    assert await until(lambda: a.state == "ready", 10)
    sim.reboot()
    assert await until(lambda: a.state == "booting", 3)
    assert await until(lambda: a.state == "ready", 10)
    assert sim.boots == 2 and sim.mission_done and not sim.aborted and sim.espnow_mode == 0
    assert a.boots == 2


async def test_nothing_is_sent_while_it_boots(rig):
    sim, a = await rig(mission=STOW)
    await until(lambda: a.state == "booting", 2)
    assert not a.send(armmod.torque(False)) and a.sent == []
    assert await until(lambda: a.state == "ready", 10)
    assert a.send(armmod.joint_deg(3, 170, 10))


async def test_hand_made_t0_bytes_are_refused_too(rig):
    sim, a = await rig()
    assert await until(lambda: a.state == "ready", 10)
    with pytest.raises(ValueError):
        a.send(b'{"T":0}\n')
    assert sim.t0_received == 0


async def test_a_silent_port_or_another_device_is_never_the_arm(rig):
    sim, a = await rig(silent=True, grace_s=0.2)
    assert await until(lambda: a.state == "lost", 8)
    assert not a.identified
    sim2, b = await rig(wrong=True, grace_s=0.2)
    assert await until(lambda: b.state == "lost", 8)
    assert not b.identified and b.feedback is None


async def test_junk_on_the_line_doesnt_stop_it(rig):
    sim, a = await rig(garbage=True)
    assert await until(lambda: a.state == "ready", 10)
    assert await until(lambda: a.fresh, 2)


async def test_it_goes_lost_when_the_arm_stops_answering_and_comes_back(rig, monkeypatch):
    monkeypatch.setattr(armmod, "LOST_S", 0.5)
    sim, a = await rig()
    assert await until(lambda: a.state == "ready", 10)
    sim.silent = True
    assert await until(lambda: a.state == "lost", 3)
    assert a.snapshot()["joints"] is None                             # stale: not shown as if current
    sim.silent = False
    assert await until(lambda: a.state == "ready", 10)


async def test_unplugged_it_is_off(rig):
    sim, a = await rig()
    assert await until(lambda: a.state == "ready", 10)
    await sim.stop()
    assert await until(lambda: a.state == "off", 5)
    assert not a.present and not a.send(armmod.read_state())
