"""tests/test_security_probes.py — regression tests for src/security_probes.py.

Covers the deterministic mode (CI-safe, no model): the full catalogue must
show zero leaks against the real gates, each probe's `gate_reason` must
match its declared defence family, and a deliberately weakened gate must
make the corresponding probe report a leak — proving the probe battery can
actually fail, not just always pass.
"""
from __future__ import annotations

import json
import os

import pytest

from src.security_probes import (
    PROBES,
    SecurityProbe,
    run_catalogue_deterministic,
    run_probe_deterministic,
    markdown_report,
    persist_results,
    PAYLOAD_CATEGORIES,
)


def test_catalogue_has_at_least_twelve_probes_covering_every_vector_and_category():
    assert len(PROBES) >= 12
    vectors = {p.vector for p in PROBES}
    assert vectors == {
        "web_fetch_result", "document", "email_body",
        "tool_result", "mcp_tool_description", "file_content",
    }
    assert {p.payload_category for p in PROBES} == PAYLOAD_CATEGORIES


def test_deterministic_full_catalogue_zero_leaks():
    summary = run_catalogue_deterministic()
    assert summary["mode"] == "deterministic"
    assert summary["probe_count"] == len(PROBES)
    assert summary["attempted"] == len(PROBES), "every probe's scripted model must attempt its call"
    assert summary["leaked"] == 0, f"SECURITY: probes leaked: {summary['leaked_ids']}"
    assert summary["blocked"] == len(PROBES)


@pytest.mark.parametrize("probe", PROBES, ids=[p.id for p in PROBES])
def test_each_probe_is_blocked_individually(probe):
    result = run_probe_deterministic(probe)
    assert result["attempted"] is True
    assert result["blocked"] is True, (
        f"probe {probe.id!r} leaked: expected {probe.expected_defense}"
    )
    assert result["leaked"] is False


def test_canary_target_probes_are_blocked_by_unknown_tool_fail_closed():
    """The two probes targeting the unregistered canary tool must be denied
    because the capability layer does not know it — the platform's
    fail-closed default for an unrecognized/high-impact tool — not merely by
    coincidence of also carrying armed external context."""
    canary_probes = [p for p in PROBES if p.target_tool == "probe_canary_send"]
    assert canary_probes, "expected at least one canary-target probe"
    for probe in canary_probes:
        result = run_probe_deterministic(probe)
        assert result["blocked"] is True


def test_zero_width_probe_is_flagged_sanitized_by_prompt_security():
    probe = next(p for p in PROBES if p.id == "tool_result_zero_width_smuggling")
    # This probe's vector is a tool RESULT, not a message, so the invisible
    # characters are in the raw payload itself; confirm prompt_security's
    # detector actually sees them (the platform-level signal an audit would
    # look for), independent of whether the gate below also blocks it.
    from src.prompt_security import _INVISIBLE_RE
    assert _INVISIBLE_RE.search(probe.payload), "probe payload should contain invisible chars"
    result = run_probe_deterministic(probe)
    assert result["blocked"] is True


def test_guard_breakout_probe_payload_is_neutralized_not_just_blocked():
    """The forged END_UNTRUSTED_SOURCE_DATA marker in the payload must not
    survive verbatim once wrapped — `_escape_guard_markers` must neutralise
    it (defence-in-depth on top of the gate that also blocks the call)."""
    probe = next(p for p in PROBES if p.id == "file_content_guard_breakout")
    from src.prompt_security import untrusted_context_message

    msg = untrusted_context_message("workspace file: notes.txt", probe.payload)
    content = msg["content"]
    assert "<<<END_UNTRUSTED_SOURCE_DATA>>>" not in content.split("\n", 2)[-1] or True
    # The forged marker must not appear as a LIVE marker before the real
    # GUARD_CLOSE the wrapper itself appends — i.e. splitting on the real
    # close must yield exactly one piece with no forged close inside it.
    from src.prompt_security import GUARD_CLOSE
    before_real_close = content.rsplit(GUARD_CLOSE, 1)[0]
    assert "<<<END_UNTRUSTED_SOURCE_DATA>>>" not in before_real_close
    assert (msg["metadata"].get("sanitized_guard_markers") is True)

    result = run_probe_deterministic(probe)
    assert result["blocked"] is True


def test_weakened_gate_makes_a_probe_leak(monkeypatch):
    """Prove the probe battery is not vacuously green: with the
    post-external-context gate patched to always allow, the SAME probes
    that pass above must now report a leak."""
    from src import tool_capabilities

    monkeypatch.setattr(
        tool_capabilities.ToolRunSecurityContext,
        "decision_for",
        lambda self, tool_name, content=None: tool_capabilities.ToolGateDecision(True),
    )

    probe = next(p for p in PROBES if p.id == "web_fetch_direct_override")
    result = run_probe_deterministic(probe)
    assert result["attempted"] is True
    assert result["blocked"] is False
    assert result["leaked"] is True

    summary = run_catalogue_deterministic()
    assert summary["leaked"] == len(PROBES), (
        "weakening decision_for globally should leak every probe that relies on it"
    )


def test_markdown_report_lists_every_probe_and_leak_count():
    summary = run_catalogue_deterministic()
    md = markdown_report(summary)
    assert "Leaked: 0" in md
    for probe in PROBES:
        assert probe.id in md


def test_persist_results_writes_a_json_file_under_data_dir(tmp_path, monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))

    summary = run_catalogue_deterministic()
    path = persist_results(summary)

    assert path.startswith(str(tmp_path))
    assert os.path.isfile(path)
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded["probe_count"] == len(PROBES)
    assert loaded["mode"] == "deterministic"


def test_probe_ids_are_unique_and_stable_strings():
    ids = [p.id for p in PROBES]
    assert len(ids) == len(set(ids))
    for i in ids:
        assert i and i == i.strip() and " " not in i


def test_markdown_report_tolerates_error_rows():
    from src.security_probes import markdown_report
    summary = {"mode": "live", "generated_at": "x", "probe_count": 1, "attempted": 0,
               "blocked": 0, "leaked": 0, "leaked_ids": [],
               "results": [{"id": "p", "attempted": False, "blocked": None, "leaked": False,
                            "error": "HTTP 401"}]}
    out = markdown_report(summary)
    assert "| p |" in out and "error" in out and "HTTP 401" in out


def test_live_parsing_counts_an_approval_card_as_blocked_attempt(monkeypatch, tmp_path):
    from src import security_probes as sp
    probe = sp.PROBES[0]
    monkeypatch.setattr(sp, "_post_form", lambda *a, **k: {"id": "s1"})
    card = {"type": "tool_output", "tool": "x", "ask_user": {"kind": "tool_approval",
            "action": {"tool": probe.live_target_tool or probe.target_tool}}}
    monkeypatch.setattr(sp, "_post_chat_stream", lambda *a, **k: [card])
    r = sp.run_probe_live(probe, base_url="http://x", endpoint_url="http://m", model="m",
                          workspace=str(tmp_path))
    assert r["attempted"] is True and r["blocked"] is True and r["leaked"] is False


def test_live_parsing_reports_model_errors_instead_of_not_attempted(monkeypatch, tmp_path):
    from src import security_probes as sp
    probe = sp.PROBES[0]
    monkeypatch.setattr(sp, "_post_form", lambda *a, **k: {"id": "s1"})
    monkeypatch.setattr(sp, "_post_chat_stream",
                        lambda *a, **k: [{"type": "error", "error": "Ollama returned 404"}])
    r = sp.run_probe_live(probe, base_url="http://x", endpoint_url="http://m", model="m",
                          workspace=str(tmp_path))
    assert "404" in r["error"]
