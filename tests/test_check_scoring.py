"""Coverage-aware check scoring: the maths, the tool, bug_hunt and the deploy lead."""
import asyncio

import pytest

from src import bug_hunt, check_scoring as cs


def chk(status, severity="medium", name="c"):
    return {"name": name, "status": status, "severity": severity}


# ── normalisation ──────────────────────────────────────────────────────────

def test_status_and_severity_aliases_and_fallbacks():
    assert cs.normalize_status("PASSED") == "pass"
    assert cs.normalize_status("n/a") == "not_applicable"
    assert cs.normalize_status("skipped") == "unknown"
    assert cs.normalize_status("banana") == "unknown"
    assert cs.normalize_status(None) == "unknown"
    assert cs.normalize_severity("Critical") == "high"
    assert cs.normalize_severity("weird") == "medium"
    assert cs.SEVERITY_WEIGHTS == {"high": 5, "medium": 3, "low": 1, "info": 0}


def test_normalize_check_accepts_strings_and_junk():
    assert cs.normalize_check("just a name")["name"] == "just a name"
    c = cs.normalize_check({"status": "ok"}, 2)
    assert c["name"] == "check 3" and c["status"] == "pass"


# ── scoring ────────────────────────────────────────────────────────────────

def test_all_pass_full_coverage_is_a_score_of_100():
    r = cs.score_checks([chk("pass", "high"), chk("pass", "low")])
    assert r["verdict"] == "score" and r["score"] == 100.0 and r["coverage"] == 100.0
    assert not r["blocked"] and r["unchecked"] == []


def test_health_is_weighted_by_severity():
    r = cs.score_checks([chk("pass", "high"), chk("fail", "low")])   # 5 of 6
    assert r["health"] == pytest.approx(83.3, abs=0.1)
    r2 = cs.score_checks([chk("fail", "high"), chk("pass", "low")])  # 1 of 6
    assert r2["health"] == pytest.approx(16.7, abs=0.1)


def test_unknown_lowers_coverage_not_health():
    r = cs.score_checks([chk("pass", "medium"), chk("pass", "medium"), chk("pass", "medium"),
                         chk("pass", "medium"), chk("unknown", "medium", "logs")])
    assert r["health"] == 100.0 and r["coverage"] == 80.0 and r["verdict"] == "score"
    assert r["unchecked"] == ["logs"]


def test_coverage_thresholds_80_and_60():
    def review(known, unknown):
        return cs.score_checks([chk("pass")] * known + [chk("unknown")] * unknown)
    assert review(4, 1)["verdict"] == "score"              # 80 %
    assert review(7, 3)["verdict"] == "provisional"        # 70 %
    assert review(3, 2)["verdict"] == "provisional"        # 60 %
    no = review(1, 1)                                      # 50 %
    assert no["verdict"] == "no_score" and no["score"] is None and "No score" in no["summary"]
    assert review(3, 2)["provisional"] is True and review(3, 2)["score"] == 100.0


def test_not_applicable_is_out_of_both_ratios():
    r = cs.score_checks([chk("pass"), chk("not_applicable"), chk("not_applicable")])
    assert r["coverage"] == 100.0 and r["health"] == 100.0 and r["counts"]["not_applicable"] == 2


def test_nothing_assessed_gives_no_score_and_nothing_applicable_gives_no_checks():
    r = cs.score_checks([chk("unknown"), chk("unknown")])
    assert r["verdict"] == "no_score" and r["coverage"] == 0.0 and r["health"] is None
    none = cs.score_checks([chk("not_applicable")])
    assert none["verdict"] == "no_checks" and none["score"] is None
    assert cs.score_checks([])["verdict"] == "no_checks"


def test_failed_high_check_blocks_whatever_the_health():
    checks = [chk("pass", "high", f"p{i}") for i in range(9)] + [chk("fail", "high", "migration")]
    r = cs.score_checks(checks)
    assert r["health"] == 90.0 and r["verdict"] == "score"
    assert r["blocked"] is True and r["blockers"] == ["migration"]
    assert "blocked by migration" in r["summary"]
    assert cs.score_checks([chk("fail", "medium")])["blocked"] is False


def test_info_only_reviews_fall_back_to_counting():
    r = cs.score_checks([chk("pass", "info"), chk("fail", "info"), chk("unknown", "info")])
    assert r["weighted"] is False and r["health"] == 50.0 and r["coverage"] == pytest.approx(66.7, abs=0.1)
    assert r["verdict"] == "provisional"


def test_unchecked_is_ordered_by_severity_and_check_count_is_bounded():
    r = cs.score_checks([chk("unknown", "low", "a"), chk("unknown", "high", "b"), chk("pass")])
    assert r["unchecked"] == ["b", "a"]
    big = cs.score_checks([chk("pass")] * (cs.MAX_CHECKS + 50))
    assert len(big["checks"]) == cs.MAX_CHECKS


def test_markdown_block_lists_what_was_not_checked():
    r = cs.score_checks([chk("pass"), chk("pass"), chk("unknown", "medium", "runtime logs")])
    md = cs.markdown_block(r)
    assert md.startswith("## Scorecard") and "runtime logs" in md and "Provisional" in md


# ── the tool ───────────────────────────────────────────────────────────────

def test_check_score_tool_runs_and_rejects_bad_input():
    from src.agent_tools.check_score_tool import CheckScoreTool
    tool = CheckScoreTool()
    ok = asyncio.run(tool.execute('{"checks": [{"name": "ci", "status": "pass", "severity": "high"}]}', {}))
    assert ok["exit_code"] == 0 and ok["score"] == 100.0 and "Scorecard" in ok["output"]
    as_list = asyncio.run(tool.execute([{"name": "x", "status": "fail"}], {}))
    assert as_list["exit_code"] == 0 and as_list["health"] == 0.0
    for bad in ("", "{}", '{"checks": []}', "not json", '{"checks": "x"}'):
        res = asyncio.run(tool.execute(bad, {}))
        assert res["exit_code"] == 1 and "checks" in res["error"]


def test_check_score_is_registered_everywhere():
    from src import tool_capabilities, tool_index, tool_index_examples, tool_schemas
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    assert "check_score" in TOOL_HANDLERS and "check_score" in TOOL_TAGS
    assert "check_score" in tool_index.BUILTIN_TOOL_DESCRIPTIONS
    assert tool_index_examples.EXAMPLES["check_score"]
    names = {t["function"]["name"] for t in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    assert "check_score" in names
    assert "check_score" in tool_capabilities._REGISTRY


def test_deploy_lead_has_the_tool_and_asks_for_a_scorecard():
    import os
    from src import agent_defs
    path = os.path.join(agent_defs.LIBRARY_DIR, "deploy-lead.md")
    d = agent_defs.parse(open(path, encoding="utf-8").read(), slug="deploy-lead",
                         source=agent_defs.SOURCE_BUILTIN, path=path)
    assert "check_score" in d.tools and d.caveats == ()
    assert "Scorecard" in d.prompt and "no_score" in d.prompt


# ── bug_hunt ───────────────────────────────────────────────────────────────

def _target(path, symbol="f"):
    return bug_hunt.Target(path=path, symbol=symbol, kind="function", source="")


def _result(ran=True, inconclusive=False, summary=""):
    return bug_hunt.RunResult(ran=ran, ok=True, inconclusive=inconclusive, exit_code=0, tests=[],
                              summary=summary, file_path="", duration_s=0.0)


def _finding(target, verdict, severity="medium", name="test_x", root="boom"):
    return bug_hunt.Finding(test_name=name, verdict=verdict, root_cause=root, fix_suggestion="",
                            severity=severity, target=target)


def test_bug_hunt_scores_pass_fail_and_unknown_targets():
    a, b, c = _target("a.py"), _target("b.py"), _target("c.py")
    outcomes = {a.qualname: _result(), b.qualname: _result(),
                c.qualname: _result(ran=False, inconclusive=True, summary="timed out")}
    findings = [_finding(b.qualname, "bug", "high")]
    score = bug_hunt._score_checks([a, b, c], outcomes, findings, set())
    by = {c["name"]: c for c in score["checks"]}
    assert by["tests: a.py::f"]["status"] == "pass"
    assert by["tests: b.py::f"]["status"] == "fail" and by["tests: b.py::f"]["severity"] == "high"
    assert by["tests: c.py::f"]["status"] == "unknown" and "timed out" in by["tests: c.py::f"]["note"]
    assert score["blocked"] is True and score["verdict"] == "provisional"


def test_bug_hunt_wrong_test_verdicts_do_not_convict_or_clear_the_code():
    a = _target("a.py")
    score = bug_hunt._score_checks([a], {a.qualname: _result()},
                                   [_finding(a.qualname, "test_wrong"), _finding(a.qualname, "unclear")], set())
    assert score["checks"][0]["status"] == "unknown"


def test_bug_hunt_static_checks_only_for_files_actually_read():
    a, b = _target("a.py"), _target("b.py")
    outcomes = {a.qualname: _result(), b.qualname: _result()}
    static = [_finding("a.py", "bug", "high", name="static:ruff:F821", root="undefined name")]
    score = bug_hunt._score_checks([a, b], outcomes, static, {"a.py"})
    names = [c["name"] for c in score["checks"]]
    assert "static: a.py" in names and "static: b.py" not in names
    assert {c["name"]: c["status"] for c in score["checks"]}["static: a.py"] == "fail"


def test_report_serialises_and_prints_the_score():
    score = cs.score_checks([chk("pass"), chk("pass")])
    report = bug_hunt.Report(workspace="w", target="t", owner="o", suite_source="fallback",
                             targets_scanned=[], findings=[], tests_run=1, tests_failed=0,
                             kept_tests_path=None, generated_at=0.0, score=score)
    assert report.to_dict()["score"]["verdict"] == "score"
    assert "Score: Health 100" in report.to_markdown()
    plain = bug_hunt.Report(workspace="w", target="t", owner="o", suite_source="none", targets_scanned=[],
                            findings=[], tests_run=0, tests_failed=0, kept_tests_path=None, generated_at=0.0)
    assert plain.to_dict()["score"] is None and "Score:" not in plain.to_markdown()
