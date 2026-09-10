"""EDIT-06 — structure-assisted refactoring: renaming a symbol via the code
index touches only real references, never an unrelated string/doc that
happens to share the substring, and produces exactly one ChangeSet.
"""
import os

import pytest

from src import code_index as ci
from src import refactor_tools as rt
from src.context_engine import store


@pytest.fixture()
def ce_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


@pytest.fixture()
def workspace(tmp_path):
    root = str(tmp_path)
    _write(root, "app/auth.py", (
        "def refresh_oauth_token(code):\n"
        "    return code.strip()\n"
        "\n"
        "def exchange(code):\n"
        "    return refresh_oauth_token(code)\n"
    ))
    _write(root, "app/notes.py", (
        "# TODO: refresh_oauth_token needs a retry (mentioned only in a comment)\n"
        "REFRESH_OAUTH_TOKEN_DOC = 'refresh_oauth_token is documented elsewhere'\n"
    ))
    return root


def test_plan_rename_reports_not_indexed_before_refresh(ce_store, workspace):
    plan = rt.plan_rename("refresh_oauth_token", "renew_oauth_token", workspace=workspace)
    assert plan["indexed"] is False
    assert "not indexed" in plan["reason"]


def test_apply_rename_refuses_when_symbol_is_not_indexed(ce_store, workspace):
    result = rt.apply_rename("refresh_oauth_token", "renew_oauth_token", workspace=workspace)
    assert result.applied is False
    assert result.files_changed == []
    assert result.changeset is None


def test_plan_rename_finds_definition_and_caller(ce_store, workspace):
    ci.refresh(workspace)
    plan = rt.plan_rename("refresh_oauth_token", "renew_oauth_token", workspace=workspace)
    assert plan["indexed"] is True
    assert plan["files"] == ["app/auth.py"]
    assert plan["reference_count"] == 2  # the def line + the one call site


def test_apply_rename_renames_definition_and_caller_only(ce_store, workspace):
    ci.refresh(workspace)
    result = rt.apply_rename("refresh_oauth_token", "renew_oauth_token", workspace=workspace,
                             owner="alice")
    assert result.applied is True
    assert result.files_changed == ["app/auth.py"]

    with open(os.path.join(workspace, "app/auth.py"), encoding="utf-8") as fh:
        auth_text = fh.read()
    assert "def renew_oauth_token(code):" in auth_text
    assert "return renew_oauth_token(code)" in auth_text
    assert "refresh_oauth_token" not in auth_text  # nothing of the old name left in the real code file

    # notes.py was NOT touched — the comment and the doc string keep the OLD
    # name verbatim, proving this did not fall back to a global substitution.
    with open(os.path.join(workspace, "app/notes.py"), encoding="utf-8") as fh:
        notes_text = fh.read()
    assert "refresh_oauth_token needs a retry" in notes_text
    assert "refresh_oauth_token is documented elsewhere" in notes_text
    assert "renew_oauth_token" not in notes_text

    assert result.changeset is not None
    cs = result.changeset.to_dict()
    assert cs["intent"] == "implement"
    assert cs["files"]["modified"] == ["app/auth.py"]


def test_apply_rename_rejects_non_identifier_new_name(ce_store, workspace):
    ci.refresh(workspace)
    with pytest.raises(rt.RefactorError):
        rt.apply_rename("refresh_oauth_token", "not a valid name", workspace=workspace)
