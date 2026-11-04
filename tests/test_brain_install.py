"""brain-host/install.sh downloads only pinned, hash-checked models and pinned pip packages."""
import re
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[1] / "brain-host" / "install.sh"
if not INSTALL.exists():
    pytest.skip("brain-host/ not in this copy of the project", allow_module_level=True)


def test_models_are_pinned_and_hash_checked():
    text = INSTALL.read_text()
    joined = text.replace("\\\n", " ")                      # one fetch call per line
    calls = [l.split()[1:] for l in joined.splitlines() if l.startswith("fetch ")]
    assert len(calls) == 4
    for name, url, sha in calls:
        assert re.fullmatch(r"[0-9a-f]{64}", sha), name
    for base in re.findall(r"^(?:ZOO|PIPER)=(\S+)", text, re.M):
        assert re.search(r"/(?:raw|resolve)/[0-9a-f]{40}/", base), base     # a commit, never a branch
    assert "/main/" not in text and "sha256sum -c" in text


def test_pip_packages_are_pinned():
    line = next(l for l in INSTALL.read_text().splitlines() if "pip\" install" in l)
    pkgs = line.split("install -q", 1)[1].split()
    assert pkgs and all(re.fullmatch(r"[a-z0-9_-]+==[0-9.]+", p) for p in pkgs), pkgs
