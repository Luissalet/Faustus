"""Tests for `src/doc_lint.py` (report-only Markdown linter) and its surfaces:
the CLI, the `doc_lint` agent tool, GET/POST /api/doc-lint (path safety) and the
`doc_lint` tool of the harness MCP server."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from src import doc_lint as dl


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "other.md").write_text("# Other\n", encoding="utf-8")
    return tmp_path


def lint(repo, text, rules=None, name="doc.md", config=None):
    path = repo / name
    path.write_text(text, encoding="utf-8")
    return dl.lint_text(text, str(path), str(repo), config or dl.load_config(str(repo)), rules)


def ids(findings):
    return [f.rule for f in findings]


# ---- headings -------------------------------------------------------------

def test_clean_document_has_no_findings(repo):
    text = "# Title\n\n## One\n\ntext\n\n### Sub\n\n## Two\n"
    assert lint(repo, text) == []


def test_heading_skip_reports_the_jump(repo):
    f = lint(repo, "# T\n\n#### Deep\n", ["heading-skip"])
    assert ids(f) == ["heading-skip"] and f[0].line == 3 and "H1" in f[0].message and f[0].fix


def test_heading_skip_going_back_up_is_fine(repo):
    assert lint(repo, "# T\n## A\n### B\n## C\n", ["heading-skip"]) == []


def test_multiple_h1(repo):
    f = lint(repo, "# A\n\n# B\n", ["heading-multiple-h1"])
    assert ids(f) == ["heading-multiple-h1"] and f[0].line == 3 and f[0].severity == "warning"


def test_missing_h1_is_info(repo):
    f = lint(repo, "## Only\n", ["heading-missing-h1"])
    assert ids(f) == ["heading-missing-h1"] and f[0].severity == "info"


def test_duplicate_heading_same_parent_only(repo):
    f = lint(repo, "# T\n## A\n### Same\n### Same\n## B\n### Same\n", ["heading-duplicate"])
    assert [x.line for x in f] == [4]


def test_headings_inside_code_fences_are_ignored(repo):
    text = "# T\n\n```\n# not a heading\n#### nor this\n```\n"
    assert lint(repo, text, ["headings"]) == []


# ---- numbering ------------------------------------------------------------

def test_numbering_gap(repo):
    f = lint(repo, "# T\n## 1. A\n## 2. B\n## 5. C\n", ["numbering-gap"])
    assert ids(f) == ["numbering-gap"] and f[0].line == 4 and "3, 4" in f[0].message


def test_numbering_duplicate_and_order(repo):
    f = lint(repo, "# T\n## 1. A\n## 2. B\n## 2. C\n## 4. D\n## 3. E\n", ["numbering"])
    kinds = {x.rule for x in f}
    assert {"numbering-duplicate", "numbering-order", "numbering-gap"} <= kinds
    dup = [x for x in f if x.rule == "numbering-duplicate"][0]
    assert dup.severity == "error" and dup.line == 4 and "line 3" in dup.message
    order = [x for x in f if x.rule == "numbering-order"][0]
    assert order.line == 6


def test_numbering_subsections_and_section_sign(repo):
    ok = "# T\n## 1. A\n### 1.1 x\n### 1.2 y\n## 2. B\n### 2.1 z\n## §3 C\n"
    assert lint(repo, ok, ["numbering"]) == []
    bad = "# T\n## 1. A\n### 1.1 x\n### 1.3 y\n"
    f = lint(repo, bad, ["numbering"])
    assert ids(f) == ["numbering-gap"] and "1.2" in f[0].message


def test_numbering_orphan_subsection(repo):
    f = lint(repo, "# T\n## 1. A\n### 3.1 lost\n", ["numbering-orphan"])
    assert ids(f) == ["numbering-orphan"] and f[0].line == 3


def test_numbering_unnumbered_headings_do_not_break_the_sequence(repo):
    assert lint(repo, "# T\n## 1. A\n## Interlude\n## 2. B\n", ["numbering"]) == []


# ---- links and paths ------------------------------------------------------

def test_broken_relative_link_and_existing_one(repo):
    text = "# T\n[ok](docs/other.md) [bad](docs/nope.md#x) [web](https://x.io/a) [a](#anchor)\n"
    f = lint(repo, text, ["link-broken"])
    assert ids(f) == ["link-broken"] and "docs/nope.md" in f[0].message and f[0].severity == "error"


def test_link_relative_to_the_document_directory(repo):
    (repo / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
    text = "# T\n[a](a.md)\n"
    assert lint(repo, text, ["link-broken"], name="docs/b.md") == []


def test_link_escaping_the_repo_is_reported_not_probed(repo):
    f = lint(repo, "# T\n[x](../../../etc/passwd)\n", ["link-broken"])
    assert ids(f) == ["link-broken"] and "outside the repository root" in f[0].message


def test_backticked_paths(repo):
    text = ("# T\n`src/real.py` `src/ghost.py` `src/real.py:12` `src/ghost_dir/` `origin/master` "
            "`read/write` `data/runtime.json` `/api/x` `src/*.py`\n")
    f = lint(repo, text, ["path-missing"])
    assert [x.message.split("'")[1] for x in f] == ["src/ghost.py", "src/ghost_dir"]


def test_links_and_paths_in_code_fences_are_ignored(repo):
    text = "# T\n```\n[x](nope.md) `src/ghost.py`\n```\n"
    assert lint(repo, text, ["links"]) == []


# ---- dates ----------------------------------------------------------------

def test_impossible_dates(repo):
    f = lint(repo, "# T\nOn 31-02-2026 and 2026-13-01 and 29-02-2025.\n", ["date-impossible"])
    assert [x.message.split("'")[1] for x in f] == ["31-02-2026", "29-02-2025", "2026-13-01"] or \
        {x.message.split("'")[1] for x in f} == {"31-02-2026", "2026-13-01", "29-02-2025"}
    assert all(x.severity == "error" for x in f)


def test_valid_dates_pass_and_leap_day_works(repo):
    assert lint(repo, "# T\n09-10-2026 2026-10-09 29-02-2028\n", ["dates"]) == []


def test_malformed_unpadded_dates(repo):
    f = lint(repo, "# T\nsee 9-10-2026 and 2026-1-5\n", ["date-malformed"])
    assert len(f) == 2 and all(x.severity == "warning" for x in f)


def test_date_year_out_of_range_is_impossible(repo):
    f = lint(repo, "# T\n01-01-1850\n", ["date-impossible"])
    assert ids(f) == ["date-impossible"]


def test_dates_in_urls_and_versions_are_not_dates(repo):
    text = "# T\nhttps://x.io/2026-13-45/a v1.2-3-4567 `31-02-2026`\n"
    assert lint(repo, text, ["dates"]) == []


# ---- required sections ----------------------------------------------------

def test_required_sections_per_document(repo):
    cfg = dl.load_config(str(repo))
    cfg["documents"]["guide.md"] = {"required_sections": ["Install", "Usage"]}
    f = lint(repo, "# G\n## 1. Install now\n", ["required-section"], name="guide.md", config=cfg)
    assert ids(f) == ["required-section"] and "Usage" in f[0].message and f[0].severity == "error"


def test_readme_and_faustus_defaults_exist():
    docs = dl.DEFAULT_CONFIG["documents"]
    assert docs["README.md"]["required_sections"] and docs["FAUSTUS.md"]["required_sections"]


def test_default_readme_requirements_apply_by_name(repo):
    f = lint(repo, "# R\n", ["required-section"], name="README.md")
    assert len(f) == len(dl.DEFAULT_CONFIG["documents"]["README.md"]["required_sections"])


# ---- forbidden content ----------------------------------------------------

def test_forbidden_trailer_default(repo):
    f = lint(repo, "# T\nfix\n\nCo-Authored-By: someone <a@b.c>\n", ["forbidden-content"])
    assert ids(f) == ["forbidden-content"] and f[0].line == 4


def test_forbidden_names_empty_by_default_and_configurable(repo):
    assert dl.DEFAULT_CONFIG["forbidden"]["names"] == []
    assert lint(repo, "# T\nAcmeCorp rocks\n", ["forbidden-content"]) == []
    cfg = dl.load_config(str(repo))
    cfg["forbidden"]["names"] = ["AcmeCorp"]
    f = lint(repo, "# T\nacmecorp rocks, AcmeCorporation not\n", ["forbidden-content"], config=cfg)
    assert [x.line for x in f] == [2]


def test_invalid_forbidden_pattern_is_reported_not_raised(repo):
    cfg = dl.load_config(str(repo))
    cfg["forbidden"]["patterns"] = ["("]
    f = lint(repo, "# T\n", ["forbidden-content"], config=cfg)
    assert ids(f) == ["forbidden-content"] and "invalid" in f[0].message


# ---- whitespace -----------------------------------------------------------

def test_whitespace_rules_are_off_by_default_and_opt_in(repo):
    text = "# T\ntrailing   \nhard break  \n\tindented\n"
    assert lint(repo, text) == []
    f = lint(repo, text, ["whitespace"])
    assert sorted(set(ids(f))) == ["tabs", "trailing-whitespace"]
    assert [x.line for x in f if x.rule == "trailing-whitespace"] == [2]


def test_config_can_turn_whitespace_on_and_rules_off(repo):
    cfg = dl.load_config(str(repo))
    cfg["whitespace"] = True
    assert "tabs" in dl.resolve_rules(None, cfg)
    cfg["disabled_rules"] = ["dates"]
    assert "date-impossible" not in dl.resolve_rules(None, cfg)


# ---- config / API shape ---------------------------------------------------

def test_repo_config_file_is_merged_over_defaults(repo):
    (repo / ".doc_lint.json").write_text(json.dumps({"forbidden": {"names": ["Zed"]}}), encoding="utf-8")
    cfg = dl.load_config(str(repo))
    assert cfg["forbidden"]["names"] == ["Zed"] and cfg["forbidden"]["patterns"]
    (repo / ".doc_lint.json").write_text("{broken", encoding="utf-8")
    assert dl.load_config(str(repo))["forbidden"]["names"] == []


def test_shipped_config_matches_the_builtin_defaults():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shipped = json.load(open(os.path.join(root, "config", "doc_lint.json"), encoding="utf-8"))
    assert shipped == dl.DEFAULT_CONFIG


def test_unknown_rule_raises(repo):
    with pytest.raises(ValueError):
        dl.resolve_rules(["nope"], dl.DEFAULT_CONFIG)


def test_finding_shape_and_sort_order(repo):
    f = lint(repo, "# T\n#### Deep\n01-01-1850\n", None)
    for x in f:
        d = x.to_dict()
        assert set(d) == {"rule", "severity", "line", "message", "fix"} and d["severity"] in dl.SEVERITIES
    assert [x.line for x in f] == sorted(x.line for x in f)


def test_lint_never_modifies_the_file(repo):
    text = "# T\n#### Deep   \n"
    path = repo / "keep.md"
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()
    dl.lint_paths([str(path)])
    assert path.read_bytes() == before


def test_lint_paths_errors_per_file(repo):
    (repo / "a.txt").write_text("x", encoding="utf-8")
    (repo / "ok.md").write_text("# T\n", encoding="utf-8")
    rep = dl.lint_paths([str(repo / "a.txt"), str(repo / "missing.md"), str(repo / "ok.md")], ["headings"])
    errs = [f for f in rep["files"] if "error" in f]
    assert len(errs) == 2 and rep["summary"]["files"] == 3 and rep["summary"]["errors_reading"] == 2


def test_resolver_refusal_is_reported_without_reading(repo):
    def deny(p):
        raise ValueError("outside")
    rep = dl.lint_paths([str(repo / "ok.md")], resolver=deny)
    assert rep["files"][0]["error"].startswith("refused")


def test_crlf_and_bom_documents(repo):
    f = lint(repo, "﻿# T\r\n\r\n#### Deep\r\n", ["heading-skip"])
    assert [x.line for x in f] == [3]


# ---- CLI --------------------------------------------------------------------

def test_cli_json_and_exit_codes(repo, capsys):
    bad = repo / "bad.md"
    bad.write_text("# T\n## 1. A\n## 1. B\n", encoding="utf-8")
    assert dl.main([str(bad), "--json", "--rules", "numbering"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["error"] == 1 and out["files"][0]["findings"][0]["rule"] == "numbering-duplicate"
    assert dl.main([str(bad), "--rules", "numbering", "--fail-on", "never"]) == 0
    assert dl.main([str(bad), "--rules", "bogus"]) == 2
    assert dl.main([str(repo / "gone.md")]) == 2
    capsys.readouterr()
    assert dl.main(["--list-rules"]) == 0
    assert "numbering-gap" in capsys.readouterr().out


def test_cli_module_entry_point(repo):
    import subprocess, sys
    good = repo / "good.md"
    good.write_text("# T\n\n## A\n", encoding="utf-8")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-m", "src.doc_lint", str(good), "--json"], cwd=root,
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["summary"]["files"] == 1


# ---- agent tool -------------------------------------------------------------

@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Allowed root `ok/` and a sibling `out/` that the path guard must refuse."""
    ok, out = tmp_path / "ok", tmp_path / "out"
    ok.mkdir()
    out.mkdir()
    monkeypatch.setattr("src.tool_execution._tool_path_roots", lambda: [str(ok)])
    (ok / "good.md").write_text("# T\n\n#### Deep\n", encoding="utf-8")
    (out / "secret.md").write_text("# S\n\n#### Deep\n", encoding="utf-8")
    return ok, out


def test_agent_tool_is_registered_everywhere_the_catalogs_look():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_capabilities import ResultIntegrity, ToolEffect, capabilities_for_tool
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    assert "doc_lint" in TOOL_TAGS and "doc_lint" in TOOL_HANDLERS
    assert len(BUILTIN_TOOL_DESCRIPTIONS["doc_lint"]) > 100 and len(EXAMPLES["doc_lint"]) >= 3
    caps = capabilities_for_tool("doc_lint")
    assert caps.known and ToolEffect.READ_WORKSPACE in caps.effects
    assert not {ToolEffect.WRITE_PRIVATE, ToolEffect.NETWORK_EGRESS, ToolEffect.EXECUTE_CODE} & set(caps.effects)
    assert caps.result_integrity == ResultIntegrity.WORKSPACE_UNTRUSTED
    fn = next(t["function"] for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "doc_lint")
    assert fn["parameters"]["required"] == ["paths"] and "rules" in fn["parameters"]["properties"]
    from src.tool_authority import AUTHORITY
    from src.tool_authority_catalog import ensure_catalog, unregistered_builtins
    ensure_catalog()
    assert "doc_lint" in AUTHORITY and "doc_lint" not in unregistered_builtins()


def _run_tool(payload):
    from src.agent_tools import TOOL_HANDLERS
    return asyncio.run(TOOL_HANDLERS["doc_lint"](payload if isinstance(payload, str) else json.dumps(payload), {}))


def test_agent_tool_lints_inside_the_workspace_and_refuses_outside(roots):
    ok, out = roots
    res = _run_tool({"paths": [str(ok / "good.md")], "rules": ["heading-skip"]})
    assert res["exit_code"] == 0 and res["report"]["summary"]["warning"] == 1
    assert "heading-skip" in res["output"]
    res = _run_tool({"paths": [str(out / "secret.md")]})
    entry = res["report"]["files"][0]
    assert entry["error"].startswith("refused") and "findings" not in entry
    assert "Deep" not in json.dumps(res)


def test_agent_tool_argument_errors(roots):
    ok, _ = roots
    assert _run_tool({})["exit_code"] == 1
    assert "unknown rule" in _run_tool({"paths": [str(ok / "good.md")], "rules": ["zzz"]})["error"]
    assert _run_tool({"paths": [f"{i}.md" for i in range(25)]})["exit_code"] == 1
    # a bare comma separated string is accepted too
    assert _run_tool(str(ok / "good.md"))["report"]["summary"]["files"] == 1


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    from routes.doc_lint_routes import setup_doc_lint_routes
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_doc_lint_routes())
    return TestClient(app)


def test_rest_get_and_post_lint_allowed_paths(client, roots):
    ok, _ = roots
    r = client.get("/api/doc-lint", params={"paths": str(ok / "good.md"), "rules": "headings"})
    assert r.status_code == 200 and r.json()["summary"]["warning"] == 1
    r = client.post("/api/doc-lint", json={"paths": [str(ok / "good.md")], "rules": ["dates"]})
    assert r.status_code == 200 and r.json()["summary"]["total"] == 0
    assert "numbering-gap" in client.get("/api/doc-lint/rules").json()["rules"]


def test_rest_refuses_paths_outside_allowed_roots(client, roots):
    ok, out = roots
    r = client.get("/api/doc-lint", params={"paths": str(out / "secret.md")})
    assert r.status_code == 403 and "outside" in r.json()["detail"]
    r = client.post("/api/doc-lint", json={"paths": [str(ok / "good.md"), str(out / "secret.md")]})
    assert r.status_code == 403  # one bad path refuses the whole request, nothing is linted
    r = client.post("/api/doc-lint", json={"paths": [str(ok / ".." / "out" / "secret.md")]})
    assert r.status_code == 403  # traversal


def test_rest_validation_errors(client, roots):
    ok, _ = roots
    assert client.get("/api/doc-lint").status_code == 400
    assert client.post("/api/doc-lint", content="nope").status_code == 400
    assert client.get("/api/doc-lint", params={"paths": str(ok / "good.md"), "rules": "zzz"}).status_code == 400
    many = ",".join(f"{i}.md" for i in range(25))
    assert client.get("/api/doc-lint", params={"paths": many}).status_code == 400


def test_rest_requires_admin(roots, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    from routes.doc_lint_routes import setup_doc_lint_routes
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    app = FastAPI()
    app.include_router(setup_doc_lint_routes())
    ok, _ = roots
    r = TestClient(app).get("/api/doc-lint", params={"paths": str(ok / "good.md")})
    assert r.status_code in (401, 403)


def test_app_mounts_the_route():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"),
               encoding="utf-8").read()
    assert "setup_doc_lint_routes" in src and "include_router(setup_doc_lint_routes())" in src


# ---- harness MCP tool -------------------------------------------------------

def test_mcp_tool_is_listed_and_lints_install_folder_files_only(tmp_path):
    pytest.importorskip("mcp")
    import mcp_servers.harness_server as srv
    tools = asyncio.run(srv.list_tools())
    assert "doc_lint" in [t.name for t in tools]
    spec = next(t for t in tools if t.name == "doc_lint")
    assert spec.inputSchema["required"] == ["paths"] and len(spec.description) > 60

    def call(args):
        return json.loads(asyncio.run(srv.call_tool("doc_lint", args))[0].text)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rep = call({"paths": ["docs/api/doc-lint.md"], "rules": ["headings"]})
    assert rep["summary"]["files"] == 1 and "error" not in rep["files"][0]
    outside = tmp_path / "x.md"
    outside.write_text("# T\n", encoding="utf-8")
    real = os.path.realpath(str(outside))
    if not any(real.startswith(os.path.realpath(r) + os.sep) for r in (root, os.getcwd())):
        assert call({"paths": [str(outside)]})["files"][0]["error"].startswith("refused")
    assert "error" in call({})
    assert "error" in call({"paths": ["README.md"], "rules": ["zzz"]})
    assert call({"paths": ["../../etc/passwd.md"]})["files"][0]["error"].startswith("refused")
