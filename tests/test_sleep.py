"""Sleep mode: schedule logic, backlight, and the server refusing to drive while asleep."""

import asyncio
from datetime import datetime

import pytest

from beamctl.sleep import Backlight, SleepManager, in_window, parse_hours
from test_integration import drive, rig, wait_for  # noqa: F401  (rig is a fixture)


def at(hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, 26, h, m)


def test_parse_and_window_across_midnight():
    hours = parse_hours("23:00-07:00")
    assert hours == (23 * 60, 7 * 60)
    assert in_window(hours, at("23:30")) and in_window(hours, at("03:00"))
    assert not in_window(hours, at("07:00")) and not in_window(hours, at("12:00"))
    day = parse_hours("13:00-14:30")
    assert in_window(day, at("14:00")) and not in_window(day, at("14:30"))
    assert parse_hours("") is None and parse_hours(None) is None
    for bad in ("23-07", "25:00-07:00", "07:00-07:00", "nonsense"):
        with pytest.raises(ValueError):
            parse_hours(bad)


def test_bad_schedule_never_raises():
    # A typo in beam.conf must not stop the motor server (the board would power the robot off).
    s = SleepManager("23:00-7")
    assert s.hours is None
    s.tick(at("23:30"), busy=False)
    assert not s.asleep


def test_schedule_sleeps_at_night_and_wakes_in_the_morning():
    s = SleepManager("23:00-07:00")
    s.tick(at("22:59"), busy=False)
    assert not s.asleep
    s.tick(at("23:00"), busy=False)
    assert s.asleep and s.reason == "schedule"
    s.tick(at("03:00"), busy=False)
    assert s.asleep
    s.tick(at("07:00"), busy=False)
    assert not s.asleep


def test_never_falls_asleep_while_someone_drives():
    s = SleepManager("23:00-07:00")
    s.tick(at("22:59"), busy=False)
    s.tick(at("23:00"), busy=True)
    s.tick(at("23:10"), busy=True)
    assert not s.asleep
    s.tick(at("23:20"), busy=False)          # they stopped: now it sleeps
    assert s.asleep


def test_woken_at_night_stays_awake_until_the_next_night():
    s = SleepManager("23:00-07:00")
    s.tick(at("22:59"), busy=False)
    s.tick(at("23:00"), busy=False)
    s.wake("a driver")
    for t in ("01:00", "05:00", "06:59", "07:00", "12:00", "22:59"):
        s.tick(at(t), busy=False)
        assert not s.asleep, t
    s.tick(at("23:00"), busy=False)
    assert s.asleep


def test_starting_up_at_night_goes_to_sleep():
    s = SleepManager("23:00-07:00")
    s.tick(at("02:00"), busy=False)
    assert s.asleep


def test_manual_sleep_lasts_until_woken_or_morning():
    s = SleepManager("23:00-07:00")
    s.tick(at("21:00"), busy=False)
    s.sleep("someone")
    s.tick(at("22:00"), busy=False)
    assert s.asleep and s.reason == "someone"
    s.tick(at("23:30"), busy=False)
    s.tick(at("07:00"), busy=False)          # the schedule wakes it however it fell asleep
    assert not s.asleep
    no_schedule = SleepManager(None)
    no_schedule.sleep("someone")
    no_schedule.tick(at("07:00"), busy=False)
    assert no_schedule.asleep                # without a schedule, only a person wakes it


def test_backlight_off_and_restored(tmp_path):
    (tmp_path / "max_brightness").write_text("93\n")
    (tmp_path / "brightness").write_text("69\n")
    s = SleepManager(None, Backlight(str(tmp_path)))
    s.sleep("test")
    assert (tmp_path / "brightness").read_text() == "0"
    s.wake("test")
    assert (tmp_path / "brightness").read_text() == "69"
    (tmp_path / "brightness").write_text("0")          # e.g. started while dark: wake to 75 %
    s2 = SleepManager(None, Backlight(str(tmp_path)))
    s2.sleep("test")
    s2.wake("test")
    assert (tmp_path / "brightness").read_text() == "69"


async def test_server_refuses_to_drive_while_asleep(rig):
    board, ctrl, client = await rig()
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "sleep"})
    assert await wait_for(lambda: ctrl.sleep.asleep)
    await drive(ws, 0.4, 0.0, 0.6)
    assert board.cmd_linear == 0.0
    status = await (await client.get("/api/status")).json()
    assert status["sleep"]["asleep"] is True
    await ws.send_json({"type": "wake"})
    assert await wait_for(lambda: not ctrl.sleep.asleep)
    await drive(ws, 0.4, 0.0, 0.6)
    assert board.cmd_linear > 0.0
    await ws.close()


def test_awake_start_relights_a_dark_screen(tmp_path):
    (tmp_path / "max_brightness").write_text("93\n")
    (tmp_path / "brightness").write_text("0\n")         # left dark by a server that died while asleep
    SleepManager(None, Backlight(str(tmp_path)))
    assert (tmp_path / "brightness").read_text() == "69"
    (tmp_path / "brightness").write_text("12\n")        # a dim screen someone chose is left alone
    SleepManager(None, Backlight(str(tmp_path)))
    assert (tmp_path / "brightness").read_text() == "12\n"


def test_reboot_while_asleep_keeps_the_real_brightness(tmp_path):
    # Seen on the robot: asleep at shutdown (brightness 0), systemd-backlight brings the screen up at its
    # 5 % minimum (4 of 93), and waking restored that 4 instead of the 69 from before.
    light = tmp_path / "bl"
    light.mkdir()
    (light / "max_brightness").write_text("93\n")
    (light / "brightness").write_text("69\n")
    state = str(tmp_path / "sleep.json")
    SleepManager(None, Backlight(str(light)), state_file=state).sleep("someone")
    (light / "brightness").write_text("4\n")                    # the reboot
    after = SleepManager(None, Backlight(str(light)), state_file=state)
    assert (light / "brightness").read_text() == "0"            # still asleep, still dark
    after.wake("someone")
    assert (light / "brightness").read_text() == "69"


def test_awake_boot_at_minimum_brightness_is_relit(tmp_path):
    (tmp_path / "max_brightness").write_text("93\n")
    (tmp_path / "brightness").write_text("4\n")
    SleepManager(None, Backlight(str(tmp_path)))
    assert (tmp_path / "brightness").read_text() == "69"


def test_sleep_is_remembered_across_restarts(tmp_path):
    state = str(tmp_path / "sleep.json")
    s = SleepManager("23:00-07:00", state_file=state)
    s.tick(at("22:00"), busy=False)
    s.tick(at("23:00"), busy=False)
    assert s.asleep
    again = SleepManager("23:00-07:00", state_file=state)       # server restarted at night
    again.tick(at("02:00"), busy=False)
    assert again.asleep
    again.tick(at("07:00"), busy=False)
    assert not again.asleep
    s = SleepManager("23:00-07:00", state_file=state)
    s.tick(at("23:30"), busy=False)
    assert s.asleep
    morning = SleepManager("23:00-07:00", state_file=state)     # restarted after the night ended
    morning.tick(at("09:00"), busy=False)
    assert not morning.asleep


def test_woken_at_night_stays_awake_after_a_restart(tmp_path):
    state = str(tmp_path / "sleep.json")
    s = SleepManager("23:00-07:00", state_file=state)
    s.tick(at("23:00"), busy=False)
    s.wake("a driver")
    again = SleepManager("23:00-07:00", state_file=state)
    again.tick(at("01:00"), busy=False)
    assert not again.asleep


def test_manual_sleep_is_remembered_and_unreadable_state_ignored(tmp_path):
    state = tmp_path / "sleep.json"
    SleepManager(None, state_file=str(state)).sleep("someone")
    assert SleepManager(None, state_file=str(state)).asleep
    state.write_text("not json")
    assert not SleepManager(None, state_file=str(state)).asleep


async def test_charging_light_set_green_and_left_alone_by_sleep(rig):
    board, ctrl, client = await rig()
    assert await wait_for(lambda: board.light == 0x0F00)        # set on connect
    ctrl.go_to_sleep("test")
    await asyncio.sleep(0.2)
    assert board.light == 0x0F00                                # the owner likes it on at night


async def test_going_to_sleep_stops_the_robot(rig):
    board, ctrl, client = await rig()
    ws = await client.ws_connect("/ws")
    await drive(ws, 0.4, 0.0, 0.6)
    assert board.cmd_linear > 0.0
    ctrl.go_to_sleep("test")
    assert await wait_for(lambda: board.cmd_linear == 0.0, timeout=1.0)
    await ws.close()
