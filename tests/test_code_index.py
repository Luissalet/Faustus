"""Tests for `src/code_index.py` (Lote 38, IDX-02/IDX-03).

Covers the module's own contract: definitions (Python via `ast`, other
languages via `repo_map`), lexical callers with a line, tests_for by
convention, the exclusion policy (default dirs, `.gitignore`,
`.faustusignore`, binary extensions, the 2 MB ceiling) and incremental
reindexing. The QA-02 acceptance scenario itself (vendor + binaries + own
code, never opened) lives in tests/qa/test_qa_02_proyecto_grande.py.
"""

import os

import pytest

from src import code_index as ci
from src.context_engine import store


@pytest.fixture()
def ce_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


AUTH_PY = '''"""Auth helpers."""


class Session:
    """A login session."""

    def refresh_oauth_token(self, code: str) -> str:
        """Exchange the code for a token."""
        return normalise(code)


def normalise(code: str) -> str:
    return code.strip()


def exchange(code: str) -> str:
    """Exchange an auth code for a token."""
    return Session().refresh_oauth_token(code)
'''

TEST_AUTH_PY = '''from app.auth import exchange


def test_exchange_strips_and_returns_token():
    assert exchange(" abc ") == "abc"
'''

APP_JS = """export function renderUser(user) {
  return formatName(user);
}

function formatName(user) {
  return user.name;
}
"""


@pytest.fixture()
def workspace(tmp_path):
    root = str(tmp_path / "repo")
    os.makedirs(root, exist_ok=True)
    write(root, "app/auth.py", AUTH_PY)
    write(root, "tests/test_auth.py", TEST_AUTH_PY)
    write(root, "static/app.js", APP_JS)
    return root


def test_finds_class_method_and_function_definitions(ce_store, workspace):
    ci.refresh(workspace)

    method = ci.find_definition("refresh_oauth_token", workspace=workspace)
    assert len(method) == 1
    assert method[0]["qualname"] == "Session.refresh_oauth_token"
    assert method[0]["kind"] == "method"
    assert method[0]["path"] == "app/auth.py"
    assert method[0]["start_line"] < method[0]["end_line"]
    assert method[0]["evidence"]["locator"]["kind"] == "lines"

    cls = ci.find_definition("Session", workspace=workspace)
    assert len(cls) == 1 and cls[0]["kind"] == "class"

    fn = ci.find_definition("exchange", workspace=workspace, kind="function")
    assert len(fn) == 1 and fn[0]["path"] == "app/auth.py"


def test_finds_lexical_callers_and_excludes_the_definition_site(ce_store, workspace):
    ci.refresh(workspace)

    callers = ci.find_callers("refresh_oauth_token", workspace=workspace)
    # `exchange` calls it; the method's own `def refresh_oauth_token(` line
    # must not come back as a call to itself.
    assert any(hit["path"] == "app/auth.py" for hit in callers)
    definition = ci.find_definition("refresh_oauth_token", workspace=workspace)[0]
    assert (definition["path"], definition["start_line"]) not in [
        (hit["path"], hit["line"]) for hit in callers
    ]


def test_tests_for_by_filename_convention_and_by_mention(ce_store, workspace):
    ci.refresh(workspace)

    by_symbol = ci.tests_for("exchange", workspace=workspace)
    assert any(hit["path"] == "tests/test_auth.py" for hit in by_symbol)

    by_path = ci.tests_for("app/auth.py", workspace=workspace)
    assert any(hit["path"] == "tests/test_auth.py" for hit in by_path)


def test_javascript_definitions_via_repo_map_extractor(ce_store, workspace):
    ci.refresh(workspace)
    hits = ci.find_definition("formatName", workspace=workspace)
    assert len(hits) == 1
    assert hits[0]["path"] == "static/app.js"
    assert hits[0]["language"] == "js"


def test_unknown_symbol_returns_empty_not_an_error(ce_store, workspace):
    ci.refresh(workspace)
    assert ci.find_definition("does_not_exist", workspace=workspace) == []
    assert ci.find_callers("does_not_exist", workspace=workspace) == []
    assert ci.tests_for("does_not_exist", workspace=workspace) == []


def test_incremental_refresh_skips_unchanged_files(ce_store, workspace):
    first = ci.refresh(workspace)
    assert first["reindexed"] == 3  # auth.py, test_auth.py, app.js

    second = ci.refresh(workspace)
    assert second["reindexed"] == 0
    assert second["symbols"] == first["symbols"]

    # Touch one file; only that file is reindexed.
    write(workspace, "app/auth.py", AUTH_PY + "\n\ndef added():\n    return 1\n")
    third = ci.refresh(workspace)
    assert third["reindexed"] == 1
    assert ci.find_definition("added", workspace=workspace)


def test_default_excluded_dirs_gitignore_faustusignore_and_binaries(ce_store, tmp_path):
    root = str(tmp_path / "repo")
    write(root, "vendor/lib.py", "def vendored():\n    return 1\n")
    write(root, "dist/bundle.py", "def built():\n    return 1\n")
    write(root, "build/out.py", "def compiled():\n    return 1\n")
    write(root, "node_modules/pkg/index.js", "function pkg() { return 1; }\n")
    write(root, ".git/config", "[core]\n")
    write(root, "ignored_by_gitignore.py", "def should_not_index():\n    return 1\n")
    write(root, ".gitignore", "ignored_by_gitignore.py\n")
    write(root, "scratch/skip_me.py", "def faustus_ignored():\n    return 1\n")
    write(root, "scratch/.faustusignore", "")  # not root-level: irrelevant, see below
    write(root, ".faustusignore", "scratch/\n")
    write(root, "app.bin", b"\x00binarydata".decode("latin-1"))
    write(root, "real/app.py", "def kept():\n    return 1\n")

    candidates = set(ci.iter_candidates(root))
    assert candidates == {"real/app.py"}

    ci.refresh(root)
    assert ci.find_definition("vendored", workspace=root) == []
    assert ci.find_definition("built", workspace=root) == []
    assert ci.find_definition("compiled", workspace=root) == []
    assert ci.find_definition("should_not_index", workspace=root) == []
    assert ci.find_definition("faustus_ignored", workspace=root) == []
    assert ci.find_definition("kept", workspace=root)


def test_files_over_2mb_are_excluded(ce_store, tmp_path):
    root = str(tmp_path / "repo")
    big = "# padding\n" * ((ci.MAX_FILE_BYTES // 10) + 1000)
    write(root, "huge.py", big + "\ndef in_a_huge_file():\n    return 1\n")
    write(root, "small.py", "def tiny():\n    return 1\n")

    candidates = set(ci.iter_candidates(root))
    assert "huge.py" not in candidates
    assert "small.py" in candidates
