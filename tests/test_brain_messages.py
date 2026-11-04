"""The brain's own robustness and privacy: a bad message from a page doesn't take it down, a second brain makes it
stand down, the logs never hold anyone's words, and the small settings and helpers behind them."""

import asyncio
import logging

import pytest

from test_brain_relay import KEY, next_of, rig  # noqa: F401  (rig is a fixture)
from test_rocky import FakeLLM

pytest.importorskip("cv2")


def new_brain():
    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore
    from beambrain.rocky import Rocky

    class FakeSpeaker:
        def wav(self, text):
            return b"RIFFpiper"

    brain = Brain(None, PeopleStore(":memory-not-used:"), FakeSpeaker(), Greeter())
    brain.rocky = Rocky(FakeLLM("[happy] Secret pancakes taste good."))
    return brain


async def test_a_bad_message_is_skipped_and_the_brain_goes_on(rig, caplog):
    _, _, client = rig
    brain = new_brain()
    stop = asyncio.Event()
    runner = asyncio.create_task(brain.run(str(client.make_url("/ws")) + f"?role=brain&key={KEY}", stop))
    try:
        robot = await client.ws_connect("/ws?role=robot")
        pilot = await client.ws_connect("/ws")
        await next_of(pilot, "people")                        # the brain is connected
        with caplog.at_level(logging.INFO, logger="beambrain"):
            await pilot.send_json({"type": "brain", "data": {"kind": "enroll", "face_id": "Alexandra", "name": "x"}})
            await pilot.send_json({"type": "brain", "data": {"kind": "chat", "text": "Tell me about pancakes"}})
            assert (await next_of(pilot, "rocky"))["text"] == "Secret pancakes taste good."   # still answering
        assert not runner.done()
        assert "skipped a message the brain couldn't handle (ValueError)" in caplog.text
        assert "Alexandra" not in caplog.text                                       # nothing from the message
        assert "pancakes" not in caplog.text.lower()                                # nor his reply, nor the chat
        assert "Rocky answered [happy], 4 words" in caplog.text
        await robot.close()
        await pilot.close()
    finally:
        stop.set()
        await runner


async def test_a_second_brain_makes_the_first_stand_down(rig):
    _, _, client = rig
    url = str(client.make_url("/ws")) + f"?role=brain&key={KEY}"
    first, second = new_brain(), new_brain()
    stop = asyncio.Event()
    pilot = await client.ws_connect("/ws")
    one = asyncio.create_task(first.run(url, stop))
    two = None
    try:
        await next_of(pilot, "people")
        two = asyncio.create_task(second.run(url, stop))
        for _ in range(100):
            if first.replaced:
                break
            await asyncio.sleep(0.05)
        assert first.replaced and not second.replaced
        await asyncio.sleep(2.5)                      # longer than its reconnect wait: it stays off
        assert first.ws is None and second.ws is not None and not second.ws.closed
        assert not one.done()                         # alive, so systemd doesn't start it again at once
    finally:
        stop.set()
        await one
        if two is not None:
            await two
        await pilot.close()


async def test_background_tasks_are_held_until_they_finish():
    brain = new_brain()
    gate = asyncio.Event()
    task = brain.spawn(gate.wait())
    assert task in brain._tasks
    gate.set()
    await task
    await asyncio.sleep(0)
    assert task not in brain._tasks


def test_brain_key_and_token_come_from_the_environment(monkeypatch, tmp_path):
    """The service passes them as $BEAM_BRAIN_KEY / $BEAM_TOKEN: a command line is readable by any program."""
    import os
    import sys

    from beambrain import brain as mod

    got = {}

    async def no_run(self, url, stop=None):
        got["url"] = url

    monkeypatch.setattr(mod.Brain, "run", no_run)
    monkeypatch.setattr(mod, "FaceEngine", lambda models: None)
    monkeypatch.setattr(mod, "Speaker", lambda models: None)
    monkeypatch.delenv("SYNTHETIC_API_KEY", raising=False)
    monkeypatch.setenv("BEAM_BRAIN_KEY", "secret-key-123")
    monkeypatch.setenv("BEAM_TOKEN", "tok-456")
    monkeypatch.setattr(sys, "argv", ["beambrain", "--url", "ws://x/ws", "--data", str(tmp_path / "data")])
    mod.main()
    assert got["url"] == "ws://x/ws?role=brain&key=secret-key-123&token=tok-456"
    assert "BEAM_BRAIN_KEY" not in os.environ and "BEAM_TOKEN" not in os.environ   # not handed to children

    monkeypatch.setattr(sys, "argv", ["beambrain", "--url", "ws://x/ws", "--data", str(tmp_path / "data")])
    with pytest.raises(SystemExit):
        mod.main()                                         # no key anywhere: a clear error


def test_household_drivers_count_without_home_assistant(monkeypatch, tmp_path):
    """--home-drivers also decides who may change Rocky's personality, so it holds without --home-url."""
    import sys

    from beambrain import brain as mod

    got = {}

    async def no_run(self, url, stop=None):
        got["brain"] = self

    monkeypatch.setattr(mod.Brain, "run", no_run)
    monkeypatch.setattr(mod, "FaceEngine", lambda models: None)
    monkeypatch.setattr(mod, "Speaker", lambda models: None)
    monkeypatch.delenv("SYNTHETIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["beambrain", "--url", "ws://x/ws", "--key", "k", "--data", str(tmp_path / "data"),
                                      "--home-drivers", "Owner@Example.com, other@example.com"])
    mod.main()
    b = got["brain"]
    assert b.home_drivers == {"owner@example.com", "other@example.com"}
    assert b.may_edit_personality("owner@example.com") and not b.may_edit_personality("guest@example.com")


async def test_ai_tasks_from_the_driving_page_wait_as_long_as_the_bake_off(monkeypatch):
    from beambrain import llm

    made = []
    real = llm.LLM.__init__

    def spy(self, *a, **kw):
        made.append(kw.get("timeout_s"))
        real(self, *a, **kw)

    monkeypatch.setattr(llm.LLM, "__init__", spy)
    brain = new_brain()
    brain.ai = {"key": "k", "model": "m"}
    sent = []
    brain.send = lambda m: asyncio.sleep(0, sent.append(m))

    await brain.start_ai_task("find the kitchen")
    brain.driver_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await brain.driver_task
    assert made == [180]


def test_espeak_never_reads_the_words_as_options(monkeypatch, tmp_path):
    from beambrain import speech

    calls = []

    class Done:
        stdout = b"RIFF"

    monkeypatch.setattr(speech.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    monkeypatch.setattr(speech.subprocess, "run", lambda args, **kw: calls.append(args) or Done())
    s = speech.Speaker(tmp_path)                       # no Piper voice there: espeak-ng
    s.voice = None
    assert s.wav("-w /tmp/x hello") == b"RIFF"
    assert calls[-1] == ["espeak-ng", "--stdout", "--", "-w /tmp/x hello"]
