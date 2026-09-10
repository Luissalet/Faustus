"""WEB-03 — browser sessions isolated by owner and by task.

MAPA_REUTILIZACION.md marks WEB-03 "ausente": the single browser profile
under DATA_DIR/browser-profile is shared by every owner/task with no
per-owner separation, no declared download/login policy, and no
close-on-task-end. `src.browser_sessions` adds that layer without touching
the existing profile (rule 3): a caller that never opens a session is
unaffected.
"""
import os

from src import browser_sessions as bs


def setup_function(_):
    bs._SESSIONS.clear()


def test_two_owners_never_share_a_profile_directory(tmp_path):
    a = bs.open_session("alice", "task-1", base_dir=str(tmp_path))
    b = bs.open_session("bob", "task-1", base_dir=str(tmp_path))
    assert a.profile_dir != b.profile_dir
    assert os.path.isdir(a.profile_dir)
    assert os.path.isdir(b.profile_dir)


def test_same_owner_and_task_reuses_the_same_profile_directory(tmp_path):
    first = bs.open_session("alice", "task-1", base_dir=str(tmp_path))
    second = bs.open_session("alice", "task-1", base_dir=str(tmp_path))
    assert first.profile_dir == second.profile_dir
    assert first is second  # idempotent while open: no widened policy sneaks in


def test_same_owner_different_tasks_get_different_profiles(tmp_path):
    t1 = bs.open_session("alice", "task-1", base_dir=str(tmp_path))
    t2 = bs.open_session("alice", "task-2", base_dir=str(tmp_path))
    assert t1.profile_dir != t2.profile_dir


def test_policy_defaults_deny_downloads_and_login_reuse(tmp_path):
    session = bs.open_session("alice", "task-1", base_dir=str(tmp_path))
    assert session.policy.allow_downloads is False
    assert session.policy.allow_logins is False
    allowed, reason = bs.policy_allows(session, "download")
    assert allowed is False
    assert "downloads are not allowed" in reason


def test_declared_policy_is_honored(tmp_path):
    session = bs.open_session("alice", "task-1", allow_downloads=True, base_dir=str(tmp_path))
    allowed, reason = bs.policy_allows(session, "download")
    assert allowed is True
    assert reason is None
    allowed, reason = bs.policy_allows(session, "reuse_login")
    assert allowed is False


def test_closing_a_task_deletes_only_that_tasks_profile(tmp_path):
    session = bs.open_session("alice", "task-1", base_dir=str(tmp_path))
    other = bs.open_session("alice", "task-2", base_dir=str(tmp_path))
    assert os.path.isdir(session.profile_dir)

    closed = bs.close_session("alice", "task-1")
    assert closed is True
    assert not os.path.exists(session.profile_dir)
    # The sibling task's profile (and, by construction, any path outside the
    # per-task directory this call computed) is untouched.
    assert os.path.isdir(other.profile_dir)


def test_closing_an_already_closed_or_unknown_session_reports_false(tmp_path):
    assert bs.close_session("nobody", "no-task") is False
    bs.open_session("alice", "task-1", base_dir=str(tmp_path))
    assert bs.close_session("alice", "task-1") is True
    assert bs.close_session("alice", "task-1") is False


def test_a_closed_session_denies_every_action():
    session = bs.open_session("alice", "task-1", allow_downloads=True, ephemeral=True)
    bs.close_session("alice", "task-1", delete_profile=False)
    closed_session = bs.get_session("alice", "task-1")
    allowed, reason = bs.policy_allows(closed_session, "download")
    assert allowed is False
    assert "closed" in reason


def test_reopening_after_close_starts_a_fresh_session(tmp_path):
    first = bs.open_session("alice", "task-1", allow_downloads=True, base_dir=str(tmp_path))
    bs.close_session("alice", "task-1")
    second = bs.open_session("alice", "task-1", allow_downloads=False, base_dir=str(tmp_path))
    assert second.is_open
    assert second.policy.allow_downloads is False  # this call's policy, not the stale one
    assert second is not first


def test_ephemeral_session_does_not_create_a_profile_directory(tmp_path):
    session = bs.open_session("alice", "task-1", ephemeral=True, base_dir=str(tmp_path))
    assert not os.path.exists(session.profile_dir)
