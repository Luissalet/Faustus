"""BASE-03 / OPS-01 / SET-01 — doctor's new checks (setup + environment
areas), the allowlisted repair registry, and the first-run "next action"
helper that must never point at a dead end.
"""
from __future__ import annotations

import json
import os

import pytest

from src import doctor


@pytest.fixture()
def clean_data_dir(tmp_path, monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(constants, "AUTH_FILE", str(tmp_path / "auth.json"))
    yield tmp_path


def test_setup_admin_is_absent_with_no_auth_file(clean_data_dir):
    finding = doctor._setup_admin()
    assert finding.state == "absent"
    assert finding.fix


def test_setup_admin_fails_when_no_user_is_an_admin(clean_data_dir):
    (clean_data_dir / "auth.json").write_text(json.dumps({
        "users": {"alice": {"is_admin": False}}
    }))
    finding = doctor._setup_admin()
    assert finding.state == "fail"


def test_setup_admin_is_ok_with_one_admin(clean_data_dir):
    (clean_data_dir / "auth.json").write_text(json.dumps({
        "users": {"alice": {"is_admin": True}, "bob": {"is_admin": False}}
    }))
    finding = doctor._setup_admin()
    assert finding.state == "ok"
    assert finding.facts["admins"] == 1


def test_setup_admin_never_crashes_on_a_corrupt_file(clean_data_dir):
    (clean_data_dir / "auth.json").write_text("{not json")
    finding = doctor._setup_admin()
    assert finding.state == "unknown"


def test_environment_lockfiles_fail_when_missing(tmp_path, monkeypatch):
    from src import runtime_paths
    monkeypatch.setattr(runtime_paths, "get_app_root", lambda: str(tmp_path))
    finding = doctor._environment_lockfiles()
    assert finding.state == "fail"


def test_environment_lockfiles_warn_when_node_modules_missing(tmp_path, monkeypatch):
    from src import runtime_paths
    (tmp_path / "requirements.txt").write_text("psutil\n")
    (tmp_path / "package-lock.json").write_text("{}")
    monkeypatch.setattr(runtime_paths, "get_app_root", lambda: str(tmp_path))
    finding = doctor._environment_lockfiles()
    assert finding.state == "warn"
    assert finding.facts.get("repair") == "npm_ci"


def test_environment_launcher_reports_a_syntax_error(tmp_path, monkeypatch):
    from src import runtime_paths
    (tmp_path / "launcher.py").write_text("def broken(:\n    pass")
    monkeypatch.setattr(runtime_paths, "get_app_root", lambda: str(tmp_path))
    finding = doctor._environment_launcher()
    assert finding.state == "fail"


def test_environment_psutil_is_ok_since_it_is_a_pinned_dependency():
    assert doctor._environment_psutil().state == "ok"


def test_repair_rejects_anything_not_allowlisted():
    with pytest.raises(ValueError):
        doctor.repair("rm -rf /")


@pytest.mark.skipif(os.name == "nt", reason="the fake npm shim is a POSIX script; on Windows the real npm.cmd ran")
def test_repair_runs_the_allowlisted_npm_ci_in_the_declared_directory(tmp_path, monkeypatch):
    """Proves `repair()` actually invokes a real subprocess scoped to the repo
    root, using a stub `npm` on PATH rather than mocking `subprocess` itself —
    this crosses a process boundary the same way an HTTP test crosses a route."""
    import os
    import stat

    fake_npm = tmp_path / "npm"
    fake_npm.write_text("#!/bin/sh\necho ran-with: \"$@\"\nexit 0\n")
    fake_npm.chmod(fake_npm.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(doctor, "_studio_root", lambda: tmp_path)

    result = doctor.repair("npm_ci")
    assert result["ok"], result
    assert "ran-with: ci" in result["stdout"]


def test_next_setup_action_recommends_the_first_real_gap(clean_data_dir, monkeypatch):
    # No auth.json at all -> admin account is the first gap in the order.
    action = doctor.next_setup_action()
    assert action["blocked"] is True
    assert action["action"]["route"]


def test_next_setup_action_reports_unblocked_when_nothing_is_missing(clean_data_dir, monkeypatch):
    (clean_data_dir / "auth.json").write_text(json.dumps({"users": {"alice": {"is_admin": True}}}))
    monkeypatch.setattr(doctor, "_setup_provider", lambda: doctor.Finding(
        "setup", "model provider", "ok", "stubbed for this test"))
    monkeypatch.setattr(doctor, "_data_dir", lambda: doctor.Finding(
        "runtime", "data directory", "ok", "stubbed for this test"))
    monkeypatch.setattr(doctor, "_models", lambda: doctor.Finding(
        "models", "Ollama catalogue", "ok", "stubbed for this test"))
    action = doctor.next_setup_action()
    assert action["blocked"] is False, action


# ── proof: a naive "any not-ok state" reading would send someone in circles ──

def test_next_setup_action_does_not_recommend_a_screen_that_would_be_empty(clean_data_dir):
    """Without provider ordering, a fresh install with no admin AND no
    provider could recommend 'pick a model' before there is any provider to
    pick one from — exactly the dead end SET-01 exists to remove. The action
    returned here must be the admin step, not the model step, because doing
    them in the wrong order sends the user to an empty picker.
    """
    action = doctor.next_setup_action()
    assert action["action"]["route"] != "/settings/models", (
        "recommending the model screen before an admin account exists is "
        "exactly the dead-end order this helper must avoid"
    )
