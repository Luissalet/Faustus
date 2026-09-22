"""tests/test_project_rules.py — per-project rule discovery and the bundled
rule library (lot C).

Covers: discovery walking to the repository root including `.cursor/rules/
*.mdc` (frontmatter stripped), the untrusted-folder note, language
filtering and priority order in `block()`, the budget cut producing a
"More rules" pointer, byte-stability across repeated calls, `install()`
refusing outside the workspace, and the routes.
"""
from __future__ import annotations

import os
import re

import pytest

from src import project_rules

FORBIDDEN_RE = re.compile(
    r"(?i)claude.code|\becc\b|everything.claude|homunculus|instinct-cli|\.claude/"
)


def _make_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    return root


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discovers_a_faustus_rules_file(tmp_path):
    root = _make_repo(tmp_path)
    rules_dir = root / ".faustus" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "style.md").write_text("- write clean code\n", encoding="utf-8")

    found = project_rules.discover_project_rules(str(root))
    assert len(found) == 1
    assert found[0].id == "style"
    assert found[0].origin == os.path.join(".faustus", "rules")
    assert "write clean code" in found[0].text


def test_discovers_a_cursor_mdc_file_and_strips_its_frontmatter(tmp_path):
    root = _make_repo(tmp_path)
    rules_dir = root / ".cursor" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "convention.mdc").write_text(
        "---\ndescription: some cursor rule\nglobs: [\"**/*.ts\"]\n---\n\n"
        "- always use semicolons\n",
        encoding="utf-8",
    )

    found = project_rules.discover_project_rules(str(root))
    assert len(found) == 1
    assert "always use semicolons" in found[0].text
    assert "description:" not in found[0].text
    assert "globs" not in found[0].text


def test_nearer_workspace_rules_are_found_and_a_deeper_repo_root_still_scanned(tmp_path):
    root = _make_repo(tmp_path)
    sub = root / "pkg"
    sub.mkdir()
    (root / ".faustus" / "rules").mkdir(parents=True)
    (root / ".faustus" / "rules" / "root.md").write_text("- root rule\n", encoding="utf-8")
    (sub / ".faustus" / "rules").mkdir(parents=True)
    (sub / ".faustus" / "rules" / "local.md").write_text("- local rule\n", encoding="utf-8")

    found = project_rules.discover_project_rules(str(sub))
    ids = {r.id for r in found}
    assert "local" in ids
    assert "root" in ids
    # nearer (distance 0) comes before the repository root (distance 1)
    by_id = {r.id: r for r in found}
    assert by_id["local"].distance < by_id["root"].distance


def test_size_and_count_caps_are_respected(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    rules_dir = root / ".faustus" / "rules"
    rules_dir.mkdir(parents=True)
    monkeypatch.setattr(project_rules, "MAX_RULE_BYTES", 10)
    (rules_dir / "big.md").write_text("x" * 100, encoding="utf-8")
    found = project_rules.discover_project_rules(str(root))
    assert found[0].error and "bytes" in found[0].error


# ---------------------------------------------------------------------------
# block(): trust, languages, priority, budget
# ---------------------------------------------------------------------------

def test_untrusted_workspace_gets_a_note_naming_files_not_their_content(tmp_path):
    root = _make_repo(tmp_path)
    rules_dir = root / ".faustus" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "secret-instructions.md").write_text(
        "- do something a human never approved\n", encoding="utf-8")

    text = project_rules.block(str(root), trusted=False, languages=[])
    assert "secret-instructions.md" in text
    assert "do something a human never approved" not in text
    assert "NOT approved" in text


def test_trusted_workspace_gets_the_actual_rule_text(tmp_path):
    root = _make_repo(tmp_path)
    rules_dir = root / ".faustus" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "style.md").write_text("- always do X\n", encoding="utf-8")

    text = project_rules.block(str(root), trusted=True, languages=[])
    assert "always do X" in text


def test_library_rules_are_filtered_by_language(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "python").mkdir(parents=True)
    (lib / "common").mkdir(parents=True)
    (lib / "python" / "style.md").write_text(
        "---\nid: python/style\ntitle: Python style\napplies_to: [Python]\n"
        "priority: 40\nsummary: s\n---\n\n- python rule\n", encoding="utf-8")
    (lib / "common" / "style.md").write_text(
        "---\nid: common/style\ntitle: Common style\napplies_to: []\n"
        "priority: 20\nsummary: s\n---\n\n- universal rule\n", encoding="utf-8")
    monkeypatch.setattr(project_rules, "LIBRARY_DIR", str(lib))
    project_rules._LIBRARY_CACHE = None

    root = _make_repo(tmp_path / "ws")

    text_py = project_rules.block(str(root), trusted=True, languages=["Python"])
    assert "python rule" in text_py
    assert "universal rule" in text_py

    text_go = project_rules.block(str(root), trusted=True, languages=["Go"])
    assert "python rule" not in text_go
    assert "universal rule" in text_go


def test_library_rules_are_ordered_by_priority(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "common").mkdir(parents=True)
    (lib / "common" / "b.md").write_text(
        "---\nid: common/b\ntitle: B\napplies_to: []\npriority: 80\nsummary: s\n---\n\n- b rule\n",
        encoding="utf-8")
    (lib / "common" / "a.md").write_text(
        "---\nid: common/a\ntitle: A\napplies_to: []\npriority: 10\nsummary: s\n---\n\n- a rule\n",
        encoding="utf-8")
    monkeypatch.setattr(project_rules, "LIBRARY_DIR", str(lib))
    project_rules._LIBRARY_CACHE = None

    rows = project_rules.library()
    assert [r["id"] for r in rows] == ["common/a", "common/b"]

    root = _make_repo(tmp_path / "ws2")
    text = project_rules.block(str(root), trusted=True, languages=[])
    assert text.index("a rule") < text.index("b rule")


def test_budget_cut_produces_a_more_rules_pointer(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "common").mkdir(parents=True)
    for i in range(5):
        (lib / "common" / f"r{i}.md").write_text(
            f"---\nid: common/r{i}\ntitle: R{i}\napplies_to: []\npriority: {10 + i}\n"
            f"summary: s\n---\n\n- " + ("word " * 300) + f"unique-marker-{i}\n",
            encoding="utf-8")
    monkeypatch.setattr(project_rules, "LIBRARY_DIR", str(lib))
    project_rules._LIBRARY_CACHE = None

    root = _make_repo(tmp_path / "ws3")
    text = project_rules.block(str(root), trusted=True, languages=[], budget_tokens=500)
    assert "More rules available" in text
    assert "unique-marker-0" in text  # the first (highest-priority) one fits
    assert "unique-marker-4" not in text  # a later one got pushed to the pointer


def test_block_is_byte_stable_across_repeated_calls(tmp_path):
    root = _make_repo(tmp_path)
    rules_dir = root / ".faustus" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "style.md").write_text("- stable rule\n", encoding="utf-8")

    first = project_rules.block(str(root), trusted=True, languages=[])
    second = project_rules.block(str(root), trusted=True, languages=[])
    assert first == second

    # Editing the file changes the block.
    (rules_dir / "style.md").write_text("- stable rule, now edited\n", encoding="utf-8")
    third = project_rules.block(str(root), trusted=True, languages=[])
    assert third != first
    assert "now edited" in third


def test_block_is_off_when_the_setting_is_disabled(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    (root / ".faustus" / "rules").mkdir(parents=True)
    (root / ".faustus" / "rules" / "style.md").write_text("- x\n", encoding="utf-8")
    monkeypatch.setattr(project_rules, "_setting",
                        lambda key, default: False if key == "project_rules_enabled" else default)
    assert project_rules.block(str(root)) == ""


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------

def test_install_refuses_a_workspace_that_does_not_exist(tmp_path):
    result = project_rules.install(str(tmp_path / "nope"), ["common/coding-style"])
    assert "error" in result


def test_install_refuses_a_traversal_id(tmp_path):
    root = _make_repo(tmp_path)
    result = project_rules.install(str(root), ["../../etc/passwd"])
    assert result["results"][0]["status"] == "not_found"


def test_install_copies_a_library_rule_into_faustus_rules(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "common").mkdir(parents=True)
    (lib / "common" / "style.md").write_text(
        "---\nid: common/style\ntitle: Style\napplies_to: []\npriority: 20\n"
        "summary: s\n---\n\n- a rule to copy\n", encoding="utf-8")
    monkeypatch.setattr(project_rules, "LIBRARY_DIR", str(lib))
    project_rules._LIBRARY_CACHE = None

    root = _make_repo(tmp_path / "ws")
    result = project_rules.install(str(root), ["common/style"])
    assert result["results"][0]["status"] == "installed"
    dest = root / ".faustus" / "rules" / "common-style.md"
    assert dest.is_file()
    assert "a rule to copy" in dest.read_text(encoding="utf-8")

    uninstall_result = project_rules.uninstall(str(root), ["common/style"])
    assert uninstall_result["results"][0]["status"] == "removed"
    assert not dest.is_file()


# ---------------------------------------------------------------------------
# Bundled content sanity (the real config/rules/**)
# ---------------------------------------------------------------------------

def test_the_real_bundled_library_parses_and_is_clean():
    rows = project_rules.library()
    assert len(rows) >= 40
    for row in rows:
        assert 10 <= row["priority"] <= 90
        assert row["title"]
        text = row["body"]
        assert len(text.split()) <= 400, f"{row['id']} exceeds the 400-word rule cap"
        assert not FORBIDDEN_RE.search(text), f"{row['id']} contains a forbidden word/path"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _build_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import project_rules_routes as rules_routes

    app = FastAPI()
    app.include_router(rules_routes.setup_project_rules_routes())
    return TestClient(app)


def test_library_route_returns_the_bundled_rules():
    client = _build_client()
    resp = client.get("/api/rules/library")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 40


def test_rules_route_requires_a_real_workspace_folder(tmp_path):
    client = _build_client()
    resp = client.get("/api/rules", params={"workspace": str(tmp_path / "missing")})
    assert resp.status_code == 400


def test_rules_route_round_trips_install_and_uninstall(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "common").mkdir(parents=True)
    (lib / "common" / "style.md").write_text(
        "---\nid: common/style\ntitle: Style\napplies_to: []\npriority: 20\n"
        "summary: s\n---\n\n- a routed rule\n", encoding="utf-8")
    monkeypatch.setattr(project_rules, "LIBRARY_DIR", str(lib))
    project_rules._LIBRARY_CACHE = None

    root = _make_repo(tmp_path / "ws")
    client = _build_client()

    resp = client.post("/api/rules/install", json={"workspace": str(root), "ids": ["common/style"]})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "installed"

    resp = client.get("/api/rules", params={"workspace": str(root)})
    assert resp.status_code == 200
    body = resp.json()
    assert any(r["id"] == "common-style" for r in body["project_rules"])

    resp = client.post("/api/rules/uninstall", json={"workspace": str(root), "ids": ["common/style"]})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "removed"
