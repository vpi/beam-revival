"""One package list for new sticks (build-image.sh) and updated ones (deploy-robot.sh)."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def packages():
    text = (ROOT / "image" / "packages.txt").read_text()
    return re.sub(r"#.*", "", text).split()


def test_package_list_has_what_the_code_needs():
    pkgs = packages()
    assert len(pkgs) == len(set(pkgs))
    for needed in ("python3-aiohttp", "adb", "chromium", "network-manager", "v4l-utils"):
        assert needed in pkgs


def test_build_and_deploy_read_the_same_list():
    build = (ROOT / "image" / "build-image.sh").read_text()
    deploy = (ROOT / "scripts" / "deploy-robot.sh").read_text()
    assert '"$HERE/packages.txt"' in build
    assert "adb" not in build                                  # no second, drifting copy of the list
    assert "-C image rootfs packages.txt" in deploy
    # Missing packages go in before the old code is replaced, so a failed install changes nothing.
    assert deploy.index("apt-get install") < deploy.index("sudo rm -rf /opt/beam-revival/beamctl")


def test_scripts_parse():
    for script in ("image/build-image.sh", "image/update-image.sh", "scripts/deploy-robot.sh"):
        subprocess.run(["bash", "-n", str(ROOT / script)], check=True)
