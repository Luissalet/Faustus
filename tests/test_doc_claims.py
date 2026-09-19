"""Tests for `src/doc_claims.py` — the deterministic doc-claim drift
checker: extraction of backticked claims (file paths, dotted symbols,
settings keys, API routes, tool names) with line/section, grounding them
against a workspace's real files, DRIFT detection when the referenced code
changed after the doc section that cites it was last edited, and the
precision rules that keep it a usable signal (settings prefix gate, symbol
module-dir gate, path runtime/gitignore/top-dir gate, historical vs.
current for FAUSTUS.md's dated sections, and the stale recency gate).

Fixture repo (tmp_path, real `git init`, explicit commit timestamps
relative to "now" so DRIFT/historical partitioning is deterministic
regardless of what day the suite runs): a Python module with a top-level
function, a `src/settings.py` with a `DEFAULT_SETTINGS` dict, a
`src/tool_schemas.py` with a `FUNCTION_TOOL_SCHEMAS` list, a
`routes/foo_routes.py` with one registered route, and a Markdown doc
citing all of them.
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from src import doc_claims as dc


# ── fixture repo helpers ─────────────────────────────────────────────

def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _git(root, *args, ts=None):
    env = os.environ.copy()
    if ts is not None:
        date = f"{ts} +0000"
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                    text=True, env=env)


def _commit(root, msg, ts):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg, ts=ts)


def _init_repo(tmp_path, files, ts, name="repo2"):
    """A fresh single-commit repo, all `files` introduced at `ts`. Kept
    separate from the shared `repo` fixture (whose base commit is always
    ~20 days old) so historical/since_days tests can pin a section's only
    edit to an arbitrary age without fighting blame's "most recent commit
    touching these lines" semantics via a non-monotonic history."""
    root = tmp_path / name
    root.mkdir()
    for rel, content in files.items():
        _write(root, rel, content)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _commit(root, "initial", ts)
    return str(root)


MODULE_PY = '''"""A module with a top-level function to reference."""


def ensure_ready(x):
    """Do the thing."""
    return x
'''

SETTINGS_PY = '''DEFAULT_SETTINGS = {
    "agent_email_confirm": True,
    "engine_idle_ttl_minutes": 30,
}
'''

TOOL_SCHEMAS_PY = '''FUNCTION_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "structural_search",
            "description": "search",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
'''

ROUTES_PY = '''from fastapi import APIRouter

router = APIRouter(prefix="/api/foo")


@router.get("/bar")
def get_bar():
    return {"ok": True}
'''

DOC_MD = '''# Demo

## 1. Section A

References `src/engine_mod.py`, `engine_mod.ensure_ready`,
`agent_email_confirm`, `/api/foo/bar` and `structural_search`.

## 2. Section B (all broken)

References `src/nope.py`, `engine_mod.not_a_symbol`,
`llm_trace_max_request_chars`, `/api/foo/missing` and `code_graph_search`.
'''

DAY = 86_400
# All fixture commits anchor to "now minus a bit", not a fixed calendar
# date, so DRIFT/historical partitioning stays correct no matter what day
# the suite runs.
T0 = int(time.time()) - 20 * DAY


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "src/engine_mod.py", MODULE_PY)
    _write(root, "src/settings.py", SETTINGS_PY)
    _write(root, "src/tool_schemas.py", TOOL_SCHEMAS_PY)
    _write(root, "routes/foo_routes.py", ROUTES_PY)
    _write(root, "FAUSTUS.md", DOC_MD)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _commit(root, "initial", T0)
    return str(root)


# ── extract_claims ───────────────────────────────────────────────────

def test_extract_claims_classifies_each_kind_with_line_and_section():
    claims = dc.extract_claims(DOC_MD, "FAUSTUS.md")
    by_text = {c.text: c for c in claims}

    assert by_text["src/engine_mod.py"].kind == "path"
    assert by_text["engine_mod.ensure_ready"].kind == "symbol"
    assert by_text["agent_email_confirm"].kind == "setting"
    assert by_text["/api/foo/bar"].kind == "route"
    assert by_text["structural_search"].kind == "tool"

    c = by_text["src/engine_mod.py"]
    assert c.doc == "FAUSTUS.md"
    assert c.section == "1. Section A"
    assert c.line == 5  # the "References `src/engine_mod.py`..." line

    c2 = by_text["/api/foo/missing"]
    assert c2.section == "2. Section B (all broken)"


def test_extract_claims_ignores_non_claim_backticks():
    text = "## S\n\nSee `foo bar` and `1.2` and `e.g.`\n"
    claims = dc.extract_claims(text, "d.md")
    assert claims == []


def test_extract_claims_is_pure_function_of_text():
    a = dc.extract_claims(DOC_MD, "FAUSTUS.md")
    b = dc.extract_claims(DOC_MD, "FAUSTUS.md")
    assert a == b


# ── check(): broken detection ────────────────────────────────────────

def test_check_finds_no_broken_claims_for_valid_references(repo):
    text = DOC_MD.split("## 2.")[0]  # section A only, all valid
    claims = dc.extract_claims(text, "FAUSTUS.md")
    findings = dc.check(claims, repo)
    assert findings == []


def test_check_detects_missing_path(repo):
    claims = [dc.Claim("path", "src/nope.py", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert len(findings) == 1
    assert findings[0].kind == "missing_path"
    assert findings[0].severity == "broken"


def test_check_detects_missing_symbol(repo):
    claims = [dc.Claim("symbol", "engine_mod.not_a_symbol", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert len(findings) == 1
    assert findings[0].kind == "missing_symbol"


def test_check_resolves_existing_symbol(repo):
    claims = [dc.Claim("symbol", "engine_mod.ensure_ready", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert findings == []


def test_check_detects_missing_setting(repo):
    claims = [dc.Claim("setting", "llm_trace_max_request_chars", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert len(findings) == 1
    assert findings[0].kind == "missing_setting"


def test_check_detects_missing_route(repo):
    claims = [dc.Claim("route", "/api/foo/missing", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert len(findings) == 1
    assert findings[0].kind == "missing_route"


def test_check_resolves_existing_route(repo):
    claims = [dc.Claim("route", "/api/foo/bar", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert findings == []


def test_check_detects_missing_tool(repo):
    claims = [dc.Claim("tool", "code_graph_search", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert len(findings) == 1
    assert findings[0].kind == "missing_tool"


def test_check_resolves_existing_tool(repo):
    claims = [dc.Claim("tool", "structural_search", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert findings == []


# ── Rule 1: settings prefix gate ─────────────────────────────────────

def test_extract_ignores_model_params_shaped_like_settings():
    """top_k / num_ctx / repeat_penalty are Ollama/model options, not
    Faustus settings keys -- they have only 1 underscore, so the shape
    gate alone already excludes them."""
    text = "## S\n\nUse `top_k`, `num_ctx` and `repeat_penalty` carefully.\n"
    claims = dc.extract_claims(text, "d.md")
    assert claims == []


def test_extract_ignores_snake_case_with_unshared_prefix():
    """>=2 underscores alone isn't enough -- the first token must be a
    prefix >=3 real DEFAULT_SETTINGS keys share. `zzzfake_` isn't one."""
    text = "## S\n\nSee `zzzfake_made_up_value`.\n"
    claims = dc.extract_claims(text, "d.md")
    assert claims == []


def test_extract_classifies_shared_prefix_as_setting():
    """`engine_` is shared by several real settings keys, so a >=2
    underscore identifier under it is a plausible settings claim even if
    this exact key doesn't exist -- check() is what decides broken."""
    text = "## S\n\nSee `engine_totally_made_up_setting`.\n"
    claims = dc.extract_claims(text, "d.md")
    assert len(claims) == 1
    assert claims[0].kind == "setting"


def test_check_flags_broken_setting_with_shared_prefix(repo):
    claims = [dc.Claim("setting", "engine_totally_made_up_setting", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert len(findings) == 1
    assert findings[0].kind == "missing_setting"


# ── Rule 2: symbol module-dir gate ───────────────────────────────────

def test_check_ignores_stdlib_and_js_dotted_calls(repo):
    claims = [
        dc.Claim("symbol", "os.walk", "FAUSTUS.md", 1, "S"),
        dc.Claim("symbol", "json.dumps", "FAUSTUS.md", 1, "S"),
        dc.Claim("symbol", "window.open", "FAUSTUS.md", 1, "S"),
    ]
    findings = dc.check(claims, repo)
    assert findings == []


def test_check_ignores_symbol_whose_module_is_nowhere_in_workspace(repo):
    claims = [dc.Claim("symbol", "nonexistent_module.some_func", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert findings == []


def test_resolve_symbol_distinguishes_ignored_from_broken(repo):
    # Not applicable (no candidate module) -> None, not the broken sentinel.
    assert dc._resolve_symbol_claim(repo, "os.walk") is None
    # Applicable module found, but the symbol isn't in it -> "" (broken).
    assert dc._resolve_symbol_claim(repo, "engine_mod.not_a_symbol") == ""
    # Applicable and found -> a real path.
    found = dc._resolve_symbol_claim(repo, "engine_mod.ensure_ready")
    assert found and found.endswith("engine_mod.py")


# ── Rule 3: path runtime/gitignore/top-dir gate ──────────────────────

def test_check_ignores_runtime_data_paths(repo):
    claims = [
        dc.Claim("path", "data/projects.json", "FAUSTUS.md", 1, "S"),
        dc.Claim("path", "web-dev-data/seed.json", "FAUSTUS.md", 1, "S"),
        dc.Claim("path", ".faustus/INSTRUCTIONS.md", "FAUSTUS.md", 1, "S"),
        dc.Claim("path", "%APPDATA%/Faustus/config.json", "FAUSTUS.md", 1, "S"),
        dc.Claim("path", "D:\\stuff\\file.py", "FAUSTUS.md", 1, "S"),
    ]
    findings = dc.check(claims, repo)
    assert findings == [], [f.to_dict() for f in findings]


def test_check_ignores_gitignored_paths(repo):
    os.makedirs(os.path.join(repo, "ignored_dir"), exist_ok=True)
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("ignored_dir/\n")
    claims = [dc.Claim("path", "ignored_dir/thing.py", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert findings == []


def test_check_ignores_paths_whose_top_dir_does_not_exist(repo):
    claims = [dc.Claim("path", "totally_missing_dir/thing.py", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert findings == []


def test_check_still_flags_missing_path_with_existing_top_dir(repo):
    # Regression: a real missing file under a real top-level dir is still
    # reported broken -- the new gates must not swallow genuine findings.
    claims = [dc.Claim("path", "src/still_missing.py", "FAUSTUS.md", 1, "S")]
    findings = dc.check(claims, repo)
    assert len(findings) == 1
    assert findings[0].kind == "missing_path"


# ── DRIFT (stale sections) ───────────────────────────────────────────

def test_report_flags_stale_when_code_changes_after_doc_section(repo):
    # Code referenced by Section A changes well after the doc's own commit,
    # but still recently (within the 90-day stale recency window).
    _write(repo, "src/engine_mod.py", MODULE_PY + "\n\n# tweak\n")
    _commit(repo, "touch engine_mod after doc", T0 + 10 * DAY)

    text = DOC_MD.split("## 2.")[0]
    _write(repo, "FAUSTUS.md", text)
    data = dc.report(repo, ["FAUSTUS.md"], since_days=30)
    stale = [f for f in data["findings"] if f["severity"] == "stale"]
    assert any(f["section"] == "1. Section A" for f in stale)
    hit = next(f for f in stale if f["section"] == "1. Section A")
    assert hit["extra"]["commits"], "expected the intervening commit to be listed"
    assert any(c["subject"] == "touch engine_mod after doc" for c in hit["extra"]["commits"])


def test_report_not_stale_when_doc_edited_after_code(repo):
    # Code changes first...
    _write(repo, "src/engine_mod.py", MODULE_PY + "\n\n# tweak\n")
    _commit(repo, "touch engine_mod", T0 + 5 * DAY)
    # ...then the doc section itself is re-edited afterwards.
    text = DOC_MD.split("## 2.")[0]
    doc_path = os.path.join(repo, "FAUSTUS.md")
    with open(doc_path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n<!-- reviewed -->\n")
    _commit(repo, "review doc", T0 + 10 * DAY)

    data = dc.report(repo, ["FAUSTUS.md"])
    stale = [f for f in data["findings"] if f["severity"] == "stale"]
    assert not any(f["section"] == "1. Section A" for f in stale)


def test_drift_commit_list_is_capped(repo):
    text = DOC_MD.split("## 2.")[0]
    _write(repo, "FAUSTUS.md", text)
    _commit(repo, "trim doc to section A", T0 + 1)
    for i in range(dc._MAX_DRIFT_COMMITS + 10):
        _write(repo, "src/engine_mod.py", MODULE_PY + f"\n# change {i}\n")
        _commit(repo, f"change {i}", T0 + 2 * DAY + i * 60)

    data = dc.report(repo, ["FAUSTUS.md"], since_days=30)
    stale = [f for f in data["findings"] if f["severity"] == "stale"
             and f["section"] == "1. Section A"]
    assert len(stale) == 1
    assert len(stale[0]["extra"]["commits"]) <= dc._MAX_DRIFT_COMMITS


# ── Rule 5: stale exclusions (other docs, recency gate) ──────────────

def test_stale_ignores_references_to_other_markdown_docs(repo):
    text = '''# Demo

## 1. Section A

See `README.md` for more.
'''
    _write(repo, "FAUSTUS.md", text)
    _commit(repo, "doc references README only", T0 + 1)
    _write(repo, "README.md", "# hi\n")
    _commit(repo, "add README", T0 + 5 * DAY)

    data = dc.report(repo, ["FAUSTUS.md"])
    stale = [f for f in data["findings"] if f["severity"] == "stale"]
    assert stale == []


def test_stale_recency_gate_suppresses_old_drift(repo):
    """Code touched after the doc section was edited, but more than
    `_STALE_MAX_AGE_DAYS` ago (relative to real "now"), must not be
    reported -- the cheap recency gate standing in for line-level
    precision (see the module docstring for why)."""
    doc_ts = int(time.time()) - 200 * DAY
    code_ts = int(time.time()) - 150 * DAY  # after doc_ts, but > 90 days old
    assert code_ts > doc_ts
    assert (int(time.time()) - code_ts) > dc._STALE_MAX_AGE_DAYS * DAY

    text = DOC_MD.split("## 2.")[0]
    _write(repo, "FAUSTUS.md", text)
    _commit(repo, "trim to section A (old)", doc_ts)
    _write(repo, "src/engine_mod.py", MODULE_PY + "\n\n# old tweak\n")
    _commit(repo, "old touch, still after doc edit", code_ts)

    data = dc.report(repo, ["FAUSTUS.md"], since_days=100_000)  # force "current"
    stale = [f for f in data["findings"] if f["severity"] == "stale"]
    assert not any(f["section"] == "1. Section A" for f in stale)


# ── Rule 4: historical vs. current (FAUSTUS.md only) ─────────────────

_OLD_SECTION_MD = '''# Demo

## 1. Old Section

References `src/nope.py`.
'''


def test_report_partitions_old_faustus_section_as_historical(tmp_path):
    old_ts = int(time.time()) - 60 * DAY  # older than default since_days=30
    root = _init_repo(tmp_path, {"FAUSTUS.md": _OLD_SECTION_MD, "src/dummy.py": "x = 1\n"}, old_ts)

    data = dc.report(root, ["FAUSTUS.md"])  # default since_days=30
    assert data["findings"] == []
    assert any(f["claim"] == "src/nope.py" for f in data["historical_findings"])
    assert data["historical_count"] == 1
    assert data["broken_count"] == 0


def test_report_all_flag_includes_historical_in_render(tmp_path):
    old_ts = int(time.time()) - 60 * DAY
    root = _init_repo(tmp_path, {"FAUSTUS.md": _OLD_SECTION_MD, "src/dummy.py": "x = 1\n"}, old_ts)

    data = dc.report(root, ["FAUSTUS.md"])
    default_render = dc.render_report(data, include_historical=False)
    all_render = dc.render_report(data, include_historical=True)
    assert "src/nope.py" not in default_render
    assert "src/nope.py" in all_render
    assert "HISTORICAL" in all_render


def test_report_readme_is_always_current_even_if_old(repo):
    old_ts = int(time.time()) - 300 * DAY
    _write(repo, "README.md", '''# Readme

## Old Section

See `src/nope.py`.
''')
    _commit(repo, "old readme", old_ts)

    data = dc.report(repo, ["README.md"])  # default since_days=30
    assert data["historical_findings"] == []
    assert any(f["claim"] == "src/nope.py" for f in data["findings"])


def test_since_days_is_configurable(tmp_path):
    old_ts = int(time.time()) - 45 * DAY
    root = _init_repo(tmp_path, {"FAUSTUS.md": _OLD_SECTION_MD, "src/dummy.py": "x = 1\n"}, old_ts)

    # 30 days (default): 45-day-old section is historical.
    default_data = dc.report(root, ["FAUSTUS.md"])
    assert any(f["claim"] == "src/nope.py" for f in default_data["historical_findings"])

    # 60 days: the same section now counts as current.
    wide_data = dc.report(root, ["FAUSTUS.md"], since_days=60)
    assert any(f["claim"] == "src/nope.py" for f in wide_data["findings"])


# ── report() / render_report() determinism ───────────────────────────

def test_report_is_deterministic(repo):
    a = dc.report(repo, ["FAUSTUS.md"])
    b = dc.report(repo, ["FAUSTUS.md"])
    assert a == b


def test_render_report_groups_by_doc_and_section(repo):
    data = dc.report(repo, ["FAUSTUS.md"])
    text = dc.render_report(data)
    assert "claims" in text or "FAUSTUS.md" in text


def test_report_handles_missing_doc(repo):
    data = dc.report(repo, ["NOPE.md"])
    assert data["docs"][0]["error"] == "not found"
    assert data["claims_total"] == 0


def test_slash_commands_are_not_routes_and_packages_resolve(tmp_path):
    from src import doc_claims as dc
    claims = dc.extract_claims("## S\nUse `/temp` and `/api/x/y`.\n", "D.md")
    kinds = {(c.kind, c.text) for c in claims}
    assert ("route", "/api/x/y") in kinds and not any(t == "/temp" for _, t in kinds)
    pkg = tmp_path / "core" / "database"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    assert dc._resolve_symbol_claim(str(tmp_path), "core.database")


def test_bare_basenames_and_subproject_paths_resolve(tmp_path):
    import subprocess as sp
    from src import doc_claims as dc
    root = tmp_path / "ws"
    (root / "studio" / "src" / "screens").mkdir(parents=True)
    (root / "studio" / "src" / "screens" / "Chat.tsx").write_text("x")
    sp.run(["git", "init", "-q"], cwd=root, check=True)
    sp.run(["git", "add", "-A"], cwd=root, check=True)
    tracked = dc._tracked_files(str(root))
    assert dc._path_resolves(str(root), "Chat.tsx", tracked)
    assert dc._path_resolves(str(root), "screens/Chat.tsx", tracked)
    assert not dc._path_resolves(str(root), "Gone.tsx", tracked)
    assert not dc._path_resolves(str(root), "hat.tsx", tracked)
