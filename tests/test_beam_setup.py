"""beam-setup on the USB image: beam.conf values it must check before using them, and Tailscale start-up.

beam-setup is a script without a .py name, so it's loaded from image/ by path. Nothing here touches the
system: every path and command it uses is pointed at tmp_path or a fake."""

import importlib.machinery
import importlib.util
import os
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "image" / "rootfs" / "usr" / "local" / "sbin" / "beam-setup"
if not SCRIPT.exists():
    pytest.skip("image/ not in this copy of the project", allow_module_level=True)


def load():
    loader = importlib.machinery.SourceFileLoader("beam_setup", str(SCRIPT))
    spec = importlib.util.spec_from_loader("beam_setup", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def bs(tmp_path, monkeypatch):
    m = load()
    monkeypatch.setattr(m, "STATE", tmp_path / "state")
    monkeypatch.setattr(m, "STAND_IN_TOKEN", tmp_path / "state" / "stand-in-token")
    monkeypatch.setattr(m, "SCREEN_KEY", tmp_path / "run" / "screen-key")
    monkeypatch.setattr(m, "SETUP", tmp_path / "setup.json")
    monkeypatch.setattr(m, "AUTHORIZED_KEYS", tmp_path / "home" / ".ssh" / "authorized_keys")
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(m.shutil, "chown", lambda *a, **k: None)      # no "beam" user here
    return m


def test_access_token_accepts_only_url_and_argv_safe_tokens(bs):
    assert bs.access_token({}) == ""
    assert bs.access_token({"ACCESS_TOKEN": ""}) == ""
    assert bs.access_token({"ACCESS_TOKEN": "abc-DEF_123"}) == "abc-DEF_123"
    for bad in ("my secret", "a+b&c#d%e", "short", "pässwörter", "x" * 129):
        assert bs.access_token({"ACCESS_TOKEN": bad}) is None, bad


def test_stand_in_token_is_valid_private_and_kept(bs):
    first = bs.stand_in_token()
    assert re.fullmatch(bs.TOKEN_RE, first)
    assert bs.STAND_IN_TOKEN.stat().st_mode & 0o777 == 0o600
    assert bs.stand_in_token() == first                 # same every boot: the robot screen's URL keeps working


def test_max_speed_typo_is_slow_and_reported(bs):
    assert bs.max_speed({}) == (0.6, None)
    assert bs.max_speed({"MAX_SPEED": "0.3"}) == (0.3, None)
    assert bs.max_speed({"MAX_SPEED": "5"}) == (1.0, None)
    assert bs.max_speed({"MAX_SPEED": "0.01"}) == (0.1, None)
    for bad in ("0,3", "fast", "inf", "nan", "1e1", "-0.5", "1_0", "0.3 m/s"):
        speed, problem = bs.max_speed({"MAX_SPEED": bad})
        assert speed == bs.SAFE_SPEED == 0.3, bad
        assert "MAX_SPEED not understood" in problem


def test_deleting_the_last_ssh_key_revokes_it(bs, capsys):
    keys = bs.AUTHORIZED_KEYS
    bs.write_authorized_keys(["ssh-ed25519 AAAA one"])
    assert keys.read_text() == "ssh-ed25519 AAAA one\n"
    assert keys.stat().st_mode & 0o777 == 0o600
    assert "1 key(s)" in capsys.readouterr().out
    bs.write_authorized_keys([])
    assert keys.read_text() == ""
    out = capsys.readouterr().out
    assert "0 key(s)" in out and "AAAA" not in out       # the count is logged, never the keys
    bs.write_authorized_keys([])
    assert capsys.readouterr().out == ""                 # unchanged: nothing to log


def config_rig(bs, tmp_path, monkeypatch, conf_text):
    """Point cmd_config at files under tmp_path, with every system command a no-op."""
    conf = tmp_path / "beam.conf"
    conf.write_text(conf_text)
    hostname = tmp_path / "hostname"
    hostname.write_text("beam\n")
    monkeypatch.setattr(bs, "CONF", conf)
    monkeypatch.setattr(bs, "SERVER_ENV", tmp_path / "etc" / "server.env")
    monkeypatch.setattr(bs, "KIOSK_URL", tmp_path / "etc" / "kiosk-url")
    real_path = bs.Path
    monkeypatch.setattr(bs, "Path", lambda p, *rest: hostname if p == "/etc/hostname" else real_path(p, *rest))

    class Done:
        returncode, stdout, stderr = 0, "beam-wifi\n", ""
    monkeypatch.setattr(bs, "run", lambda *cmd, **kw: Done())


def test_config_bad_token_keeps_the_server_locked(bs, tmp_path, monkeypatch):
    config_rig(bs, tmp_path, monkeypatch, "WIFI_SSID=home\nACCESS_TOKEN=my secret\nMAX_SPEED=0,3\n")
    bs.cmd_config()
    env = bs.SERVER_ENV.read_text()
    stand_in = bs.STAND_IN_TOKEN.read_text()
    sk = bs.SCREEN_KEY.read_text()
    assert env == f"BEAM_ARGS=--host 127.0.0.1 --max-linear 0.3\nBEAM_TOKEN={stand_in}\nBEAM_SCREEN_KEY={sk}\n"
    assert "my secret" not in env and stand_in != "my secret"     # not "my": a random token can contain it
    assert bs.KIOSK_URL.read_text() == f"http://localhost:8080/robot?token={stand_in}&screen_key={sk}\n"
    message = bs.json.loads(bs.SETUP.read_text())["message"]
    assert "ACCESS_TOKEN must be" in message and "MAX_SPEED not understood" in message
    assert "secret" not in message


def test_an_empty_beam_conf_leaves_the_ssh_keys(bs, tmp_path, monkeypatch):
    bs.write_authorized_keys(["ssh-ed25519 AAAA one"])
    config_rig(bs, tmp_path, monkeypatch, "")                   # e.g. a failed save truncated it
    bs.cmd_config()
    assert bs.AUTHORIZED_KEYS.read_text() == "ssh-ed25519 AAAA one\n"
    config_rig(bs, tmp_path, monkeypatch, "WIFI_SSID=home\n")    # a real config with no keys: revoked
    bs.cmd_config()
    assert bs.AUTHORIZED_KEYS.read_text() == ""


def test_config_good_token(bs, tmp_path, monkeypatch):
    config_rig(bs, tmp_path, monkeypatch, "WIFI_SSID=home\r\nACCESS_TOKEN=Good-token_42\r\nMAX_SPEED=0.4\r\n")
    bs.cmd_config()
    sk = bs.SCREEN_KEY.read_text()
    assert bs.SERVER_ENV.read_text() == f"BEAM_ARGS=--host 127.0.0.1 --max-linear 0.4\nBEAM_TOKEN=Good-token_42\nBEAM_SCREEN_KEY={sk}\n"
    assert bs.KIOSK_URL.read_text() == f"http://localhost:8080/robot?token=Good-token_42&screen_key={sk}\n"
    assert not bs.STAND_IN_TOKEN.exists()
    assert "message" not in bs.json.loads(bs.SETUP.read_text())


def test_secrets_stay_off_the_server_command_line(bs, tmp_path, monkeypatch):
    """Any program can read a command line: the token and brain key go in their own root-only variables."""
    key = "k" * 20
    config_rig(bs, tmp_path, monkeypatch, f"WIFI_SSID=home\nACCESS_TOKEN=Good-token_42\nBRAIN_KEY={key}\n")
    chowned = []
    monkeypatch.setattr(bs.shutil, "chown", lambda path, *a, **k: chowned.append((path, a)))
    bs.cmd_config()
    lines = bs.SERVER_ENV.read_text().splitlines()
    args = next(l for l in lines if l.startswith("BEAM_ARGS="))
    assert "Good-token_42" not in args and key not in args and "--token" not in args and "--brain-key" not in args
    assert "BEAM_TOKEN=Good-token_42" in lines and f"BEAM_BRAIN_KEY={key}" in lines
    for f in (bs.SERVER_ENV, bs.KIOSK_URL):
        assert f.stat().st_mode & 0o077 == 0, f
    assert (bs.KIOSK_URL, ("kiosk",)) in chowned
    assert not list(bs.SERVER_ENV.parent.glob("*.tmp"))


def test_the_robot_screen_keeps_the_token_off_command_lines():
    kiosk = (SCRIPT.parents[1] / "bin" / "beam-kiosk").read_text()
    assert '"$URL"' not in kiosk.split("cage ", 1)[1]           # Chromium opens a private start page instead
    assert 'curl -fs -o /dev/null "$STATUS"' not in kiosk and 'curl -fs "$STATUS"' not in kiosk
    assert "curl -fs -K -" in kiosk and "umask 077" in kiosk


def tailscale_rig(bs, tmp_path, monkeypatch, states, conf_text=""):
    """Fake `tailscale status`: one state per call; the rest of the commands are recorded."""
    conf = tmp_path / "beam.conf"
    conf.write_text(conf_text)
    monkeypatch.setattr(bs, "CONF", conf)
    states = list(states)

    def status():
        return {"BackendState": states.pop(0) if len(states) > 1 else states[0],
                "Self": {"DNSName": "beam.example.ts.net."}}
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(*cmd, **kw):
        calls.append(cmd)
        return Done()
    monkeypatch.setattr(bs, "ts_status", status)
    monkeypatch.setattr(bs, "run", run)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    return calls


def test_tailscale_stopped_is_brought_back_up(bs, tmp_path, monkeypatch):
    calls = tailscale_rig(bs, tmp_path, monkeypatch, ["Stopped", "Stopped", "Running"])
    bs.cmd_tailscale()
    ups = [c for c in calls if c[:2] == ("tailscale", "up")]
    assert ups == [("tailscale", "up", "--hostname=beam")]          # once, not every 3 s
    assert bs.json.loads(bs.SETUP.read_text())["url"] == "https://beam.example.ts.net/"


def test_tailscale_url_never_carries_an_unusable_token(bs, tmp_path, monkeypatch):
    tailscale_rig(bs, tmp_path, monkeypatch, ["Running"], "ACCESS_TOKEN=a+b&c#d%e\n")
    bs.cmd_tailscale()
    assert bs.json.loads(bs.SETUP.read_text())["url"] == "https://beam.example.ts.net/"
    tailscale_rig(bs, tmp_path, monkeypatch, ["Running"], "ACCESS_TOKEN=Good-token_42\n")
    bs.cmd_tailscale()
    assert bs.json.loads(bs.SETUP.read_text())["url"] == "https://beam.example.ts.net/?token=Good-token_42"


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_backup_is_root_only(bs, tmp_path, monkeypatch):
    disk = tmp_path / "disk"
    disk.write_bytes(b"internal disk")
    monkeypatch.setattr(bs, "internal_disk", lambda: {"name": "sda", "size": "13", "model": "M", "serial": "S"})
    real_popen = bs.subprocess.Popen

    def popen(cmd, **kw):                    # dd reads our fake disk instead of /dev/sda
        return real_popen(["cat", str(disk)], **kw)
    monkeypatch.setattr(bs.subprocess, "Popen", popen)
    monkeypatch.setattr(bs.subprocess, "run", lambda cmd, stdin, stdout: bs.subprocess.CompletedProcess(
        cmd, real_popen(["cat"], stdin=stdin, stdout=stdout).wait()))
    old_umask = os.umask(0o022)                  # the default that used to leave it world-readable
    try:
        bs.cmd_backup()
    finally:
        os.umask(old_umask)
    dest = bs.STATE / "backup"
    assert dest.stat().st_mode & 0o777 == 0o700
    image = dest / "M-S.img.zst"
    assert image.read_bytes() == b"internal disk"
    assert image.stat().st_mode & 0o777 == 0o600


def test_screen_key_is_made_once_per_boot_and_reaches_screen_server_and_panel(bs, tmp_path, monkeypatch):
    """Only the robot screen and the phone panel may take their roles: not any app that reaches 127.0.0.1."""
    config_rig(bs, tmp_path, monkeypatch, "WIFI_SSID=home\n")
    bs.cmd_config()
    sk = bs.SCREEN_KEY.read_text()
    assert re.fullmatch(bs.TOKEN_RE, sk) and len(sk) >= 32
    assert bs.SCREEN_KEY.stat().st_mode & 0o077 == 0
    assert f"BEAM_SCREEN_KEY={sk}" in bs.SERVER_ENV.read_text().splitlines()
    assert bs.KIOSK_URL.read_text() == f"http://localhost:8080/robot?screen_key={sk}\n"
    assert bs.panel_url() == f"http://localhost:8080/panel?screen_key={sk}"
    bs.cmd_config()                                             # beam.conf applied again: same key all boot
    assert bs.SCREEN_KEY.read_text() == sk
    bs.SCREEN_KEY.unlink()                                      # /run emptied: a new boot, a new key
    bs.cmd_config()
    assert bs.SCREEN_KEY.read_text() != sk


def test_the_robot_screen_reads_its_url_after_the_server_is_up():
    """The server starts after beam-config wrote this boot's key; the kiosk login may have started earlier."""
    kiosk = (SCRIPT.parents[1] / "bin" / "beam-kiosk").read_text()
    wait = kiosk.index("status -o /dev/null && break")
    assert kiosk.index("> \"$START_PAGE\"") > wait and kiosk.rindex("URL=$(cat /etc/beam/kiosk-url") > wait


def test_sensor_ports(bs):
    assert bs.serial_device({}, "LIDAR_DEVICE") == ("auto", None)
    assert bs.serial_device({"LIDAR_DEVICE": " Auto "}, "LIDAR_DEVICE") == ("auto", None)
    assert bs.serial_device({"RANGE_DEVICE": "none"}, "RANGE_DEVICE") == ("none", None)
    name = "usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_0123abcd-if00-port0"
    assert bs.serial_device({"LIDAR_DEVICE": name}, "LIDAR_DEVICE") == ("/dev/serial/by-id/" + name, None)
    assert bs.serial_device({"LIDAR_DEVICE": "/dev/ttyUSB1"}, "LIDAR_DEVICE") == ("/dev/ttyUSB1", None)
    for bad in ("/dev/tty USB1", "/etc/passwd", "/dev/../etc/shadow", "x; rm -rf /", "$(id)"):
        device, problem = bs.serial_device({"LIDAR_DEVICE": bad}, "LIDAR_DEVICE")
        assert device == "auto" and "LIDAR_DEVICE not understood" in problem, bad   # never "none"


def test_several_distance_sensors(bs):
    conf = {"RANGE_DEVICE": "usb-a-if00@-30, usb-b-if00 ,usb-c-if00@+30"}
    by = "/dev/serial/by-id/"
    assert bs.serial_device(conf, "RANGE_DEVICE") == (f"{by}usb-a-if00@-30,{by}usb-b-if00,{by}usb-c-if00@30", None)
    for bad in ("usb-a@left", "usb-a@200", "usb-a,,usb-b", "usb-a@-30;id", "usb-a@"):
        device, problem = bs.serial_device({"RANGE_DEVICE": bad}, "RANGE_DEVICE")
        assert device == "auto" and "several with commas" in problem, bad
    device, _ = bs.serial_device({"LIDAR_DEVICE": "usb-a,usb-b"}, "LIDAR_DEVICE")
    assert device == "auto"                                       # one lidar only


def test_sensor_ports_reach_the_server(bs, tmp_path, monkeypatch):
    config_rig(bs, tmp_path, monkeypatch, "WIFI_SSID=home\nLIDAR_DEVICE=usb-lidar-if00\nRANGE_DEVICE=none\n")
    bs.cmd_config()
    args = next(l for l in bs.SERVER_ENV.read_text().splitlines() if l.startswith("BEAM_ARGS="))
    assert args.endswith(" --lidar-device /dev/serial/by-id/usb-lidar-if00 --range-device none")


def test_the_8x8_sensor_settings_reach_the_server(bs, tmp_path, monkeypatch):
    config_rig(bs, tmp_path, monkeypatch, "WIFI_SSID=home\nMATRIX_DEVICE=none\nMATRIX_HEIGHT_CM=12\n"
                                          "MATRIX_TILT_DEG=15\nMATRIX_FLIP=X\n")
    bs.cmd_config()
    args = next(l for l in bs.SERVER_ENV.read_text().splitlines() if l.startswith("BEAM_ARGS="))
    assert args.endswith(" --matrix-device none --matrix-height 0.12 --matrix-tilt 15 --matrix-flip x")
    assert bs.matrix_mount({}) == ("", [])
    for bad in ({"MATRIX_HEIGHT_CM": "12; reboot"}, {"MATRIX_TILT_DEG": "90"}, {"MATRIX_FLIP": "z"}):
        args, problems = bs.matrix_mount(bad)
        assert args == "" and len(problems) == 1, bad


def test_the_robot_width_and_obstacle_distances_reach_the_server(bs, tmp_path, monkeypatch):
    config_rig(bs, tmp_path, monkeypatch, "WIFI_SSID=home\nROBOT_WIDTH_CM=31\nLIDAR_SIDE_CM=-3\n"
                                          "OBSTACLE_STOP_CM=12\nOBSTACLE_SLOW_CM=50.5\n")
    bs.cmd_config()
    args = next(l for l in bs.SERVER_ENV.read_text().splitlines() if l.startswith("BEAM_ARGS="))
    assert args.endswith(" --robot-half-width 0.18 --lidar-side -0.03 --obstacle-stop-m 0.12 --obstacle-slow-m 0.505")
    assert bs.obstacle_settings({}) == ("", [])
    for bad in ({"ROBOT_WIDTH_CM": "44; reboot"}, {"ROBOT_WIDTH_CM": "200"}, {"OBSTACLE_STOP_CM": "-5"},
                {"OBSTACLE_SLOW_CM": "1e3"}, {"LIDAR_SIDE_CM": "20"}, {"LIDAR_SIDE_CM": "left"}):
        args, problems = bs.obstacle_settings(bad)
        assert args == "" and len(problems) == 1, bad
