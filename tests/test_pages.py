"""The web pages' scripts: they parse, and the safety rules the driving page and the phone panel keep."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "beamctl" / "web"


def script(page: str) -> str:
    """The page's inline JavaScript."""
    return "\n".join(re.findall(r"<script>(.*?)</script>", (WEB / page).read_text(encoding="utf-8"), re.S))


@pytest.mark.parametrize("page", ["index.html", "panel.html", "robot.html"])
def test_page_script_parses(page, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node here to check the JavaScript")
    js = tmp_path / (page + ".js")
    js.write_text(script(page), encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_every_dialog_on_the_driving_page_lets_go_of_the_controls_first():
    # A dialog takes the key-up of a held drive key; ask()/askText() release everything (and stop) first.
    js = script("index.html")
    code = re.sub(r"//.*", "", js)                         # not the comments
    bare = re.findall(r"(?<![\w.])(confirm|prompt)\(", code)
    assert bare == ["confirm", "prompt"], bare            # only inside ask() and askText() themselves
    assert "function ask(text) { releaseAll(); return confirm(text); }" in js
    assert "function askText(text) { releaseAll(); return prompt(text); }" in js
    assert re.search(r"addEventListener\(\"blur\", releaseAll\)", js)
    assert "if (document.hidden) releaseAll()" in js


def test_letting_go_sends_a_stop_at_once():
    js = script("index.html")
    body = js[js.index("function releaseAll()"):js.index('addEventListener("blur"')]
    assert 'send({ type: "stop" })' in body


def test_gamepad_needs_the_standard_layout_and_sticks_seen_at_rest():
    js = script("index.html")
    body = js[js.index("function readGamepad()"):js.index("// ---------- send loop")]
    assert 'gp.mapping !== "standard"' in body
    assert "rest.axes.has(i) ? v : 0" in body              # an axis never seen at centre doesn't drive
    assert "input.pad = [-axis(1), -axis(2)]" in body      # left stick speed, right stick turn
    assert "gp.buttons[1]" in body and "setEstop(true)" in body   # B is still the E-stop


def test_only_the_page_that_pressed_stop_or_the_driver_offers_release():
    js = script("index.html")
    assert '"STOPPED by another page"' in js and '"STOPPED · tap to release"' in js
    assert 'said("estop_by_you")' in js and 'said("can_release_estop")' in js   # the server's word, if given
    body = js[js.index("function setEstop(on)"):js.index('$("estop").addEventListener')]
    assert 'said("can_release_estop") === false' in body
    assert "Someone else pressed STOP" in body             # anyone but the page that pressed it confirms first


def test_a_lost_obstacle_sensor_is_shown():
    js = script("index.html")
    assert "O && O.lost" in js and "LOST, crawling" in js and "Obstacle sensor lost" in js


def test_panel_names_everyone_who_gets_the_camera_or_microphone():
    js = script("panel.html")
    assert "M.brain_online" in js and "M.listening" in js
    assert '"📷 Nobody is watching"' in js


def test_panel_opens_one_camera_at_a_time_and_closes_replaced_peers():
    js = script("panel.html")
    assert "opening = opening ||" in js
    assert "if (old) old.close();" in js
    assert "hellos.get(from) !== n" in js
