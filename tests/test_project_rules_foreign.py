"""Rule files other tools keep: .clinerules, .windsurf/rules, .windsurfrules,
.github/instructions/*.instructions.md and GEMINI.md."""
from __future__ import annotations

import pytest

from src import project_instructions as pi
from src import project_rules as pr


@pytest.fixture(autouse=True)
def _fresh():
    pr.DELIVERED.reset()
    yield
    pr.DELIVERED.reset()


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _put(root, rel, text):
    p = root.joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _rules(root):
    return {r.id: r for r in pr.discover_project_rules(str(root))}


# ---- single-file instructions -------------------------------------------------

def test_new_single_files_are_candidates_after_the_existing_ones():
    names = list(pi.DEFAULT_FILES)
    for n in (".windsurfrules", ".clinerules", "GEMINI.md"):
        assert n in names
    assert names.index("AGENTS.md") < names.index("GEMINI.md")
    assert names.index(".cursorrules") < names.index(".windsurfrules")


@pytest.mark.parametrize("name", ["GEMINI.md", ".windsurfrules", ".clinerules"])
def test_single_file_is_read_as_project_instructions(tmp_path, name):
    root = _repo(tmp_path)
    _put(root, name, "- run the tests with make check\n")
    info = pi.read(str(root))
    assert info["rel"] == name and "make check" in info["text"]
    assert "make check" in pi.block(str(root))


def test_existing_file_still_wins_over_a_foreign_one(tmp_path):
    root = _repo(tmp_path)
    _put(root, "GEMINI.md", "gemini text")
    _put(root, "AGENTS.md", "agents text")
    assert pi.read(str(root))["rel"] == "AGENTS.md"


def test_clinerules_directory_is_not_mistaken_for_the_file(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".clinerules/style.md", "- small functions\n")
    assert pi.find_file(str(root)) is None


# ---- rule folders ---------------------------------------------------------------

def test_clinerules_folder_rules_are_always_on(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".clinerules/01-style.md", "- small functions\n")
    _put(root, ".clinerules/notes.txt", "- plain text rule\n")
    rules = _rules(root)
    assert rules["01-style"].paths == () and not rules["01-style"].manual
    assert rules["01-style"].text == "- small functions"
    assert "notes" in rules
    block = pr.block(str(root), trusted=True)
    assert "small functions" in block and "plain text rule" in block


def test_windsurf_triggers(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".windsurf/rules/always.md", "---\ntrigger: always_on\n---\n- be terse\n")
    _put(root, ".windsurf/rules/scoped.md", "---\ntrigger: glob\nglobs: **/*.py, scripts/*.sh\n---\n- type hints\n")
    _put(root, ".windsurf/rules/manual.md", "---\ntrigger: manual\ndescription: release checklist\n---\n- tag first\n")
    _put(root, ".windsurf/rules/decide.md", "---\ntrigger: model_decision\ndescription: when touching db code\n---\n- migrate\n")
    _put(root, ".windsurf/rules/bare.md", "- no frontmatter at all\n")
    r = _rules(root)
    assert r["always"].paths == () and not r["always"].manual and r["always"].text == "- be terse"
    assert r["scoped"].paths == ("**/*.py", "scripts/*.sh") and not r["scoped"].manual
    assert r["manual"].manual and r["manual"].description == "release checklist"
    assert r["decide"].manual
    assert not r["bare"].manual and r["bare"].paths == ()
    # glob trigger without any globs cannot be scoped: it is manual
    _put(root, ".windsurf/rules/emptyglob.md", "---\ntrigger: glob\n---\n- x\n")
    assert _rules(root)["emptyglob"].manual


def test_copilot_instructions_apply_to(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".github/instructions/ts.instructions.md",
         '---\napplyTo: "**/*.ts,**/*.tsx"\n---\n- prefer unknown to any\n')
    _put(root, ".github/instructions/all.instructions.md", "---\napplyTo: '**'\n---\n- say why\n")
    _put(root, ".github/instructions/loose.instructions.md", "- attach me when needed\n")
    _put(root, ".github/instructions/readme.md", "not an instructions file\n")
    r = _rules(root)
    assert set(r) == {"ts", "all", "loose"}                       # id drops `.instructions.md`
    assert r["ts"].paths == ("**/*.ts", "**/*.tsx") and not r["ts"].manual
    assert r["all"].paths == () and not r["all"].manual           # every file: always on
    assert r["loose"].manual                                       # no applyTo: not automatic


def test_always_apply_and_globs_dialect(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".windsurf/rules/a.md", "---\nalwaysApply: true\nglobs: src/**\n---\n- always\n")
    _put(root, ".windsurf/rules/b.md", "---\nglobs:\n  - a/**\n  - b/**\n---\n- scoped\n")
    r = _rules(root)
    assert r["a"].paths == () and not r["a"].manual
    assert r["b"].paths == ("a/**", "b/**")


def test_only_always_on_rules_go_into_the_cached_prompt(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".windsurf/rules/always.md", "---\ntrigger: always_on\n---\n- ALWAYS-TEXT\n")
    _put(root, ".windsurf/rules/scoped.md", "---\ntrigger: glob\nglobs: src/**\n---\n- SCOPED-TEXT\n")
    _put(root, ".windsurf/rules/manual.md", "---\ntrigger: manual\ndescription: ship it\n---\n- MANUAL-TEXT\n")
    block = pr.block(str(root), trusted=True)
    assert "ALWAYS-TEXT" in block
    assert "SCOPED-TEXT" not in block and "MANUAL-TEXT" not in block
    assert "scoped.md" in block and "Path-scoped rules" in block           # listed, inactive
    assert "Manual rules" in block and "manual.md (ship it)" in block      # listed, inactive


def test_glob_scoped_foreign_rule_activates_on_a_matching_file(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".github/instructions/py.instructions.md", "---\napplyTo: src/**/*.py\n---\n- PY-RULE\n")
    rules = pr.discover_project_rules(str(root))
    note = pr.path_rule_note("conv-1", rules, [str(root / "src" / "app" / "m.py")])
    assert "PY-RULE" in note
    assert pr.path_rule_note("conv-1", rules, [str(root / "src" / "app" / "m.py")]) == ""     # once
    assert pr.path_rule_note("conv-2", rules, [str(root / "docs" / "x.md")]) == ""


def test_manual_rule_is_never_delivered_by_a_file_touch(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".windsurf/rules/m.md", "---\ntrigger: manual\n---\n- MANUAL-TEXT\n")
    rules = pr.discover_project_rules(str(root))
    assert pr.path_rule_note("c", rules, [str(root / "a.py")]) == ""
    assert rules[0].to_dict()["manual"] is True


def test_untrusted_folder_names_the_files_but_not_their_text(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".clinerules/x.md", "- SECRET-POLICY\n")
    text = pr.block(str(root), trusted=False)
    assert ".clinerules/x.md" in text and "SECRET-POLICY" not in text


def test_existing_origins_keep_their_dialect(tmp_path):
    root = _repo(tmp_path)
    _put(root, ".cursor/rules/s.mdc", '---\nglobs: ["**/*.ts"]\nalwaysApply: false\n---\n- use strict\n')
    _put(root, ".faustus/rules/n.md", "---\ntitle: Style\n---\n\n- keep it short")
    r = _rules(root)
    assert r["s"].paths == ("**/*.ts",) and not r["s"].manual
    assert r["n"].text.startswith("---") and not r["n"].manual


def test_signature_changes_when_a_rule_turns_manual(tmp_path):
    root = _repo(tmp_path)
    p = _put(root, ".windsurf/rules/r.md", "---\ntrigger: always_on\n---\n- x\n")
    before = pr._project_rules_signature(str(root))
    p.write_text("---\ntrigger: manual\n---\n- x\n", encoding="utf-8")
    assert pr._project_rules_signature(str(root)) != before