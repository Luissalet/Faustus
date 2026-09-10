"""CTX-06 — salud del contexto sin otro LLM obligatorio.

Three deterministic signals (`src/context_health.py`), each provable without
a model call or a real filesystem: repetition, stale evidence (reusing
CTX-03's own `EvidenceRef`/`verify_read_evidence` contract) and tool-output
ratio (reusing `context_ledger.build_ledger`'s section counts). Plus
`reconstruct_task`, the "reconstruir tarea" button's backend: goal,
constraints, changes and next_action recovered from whatever a compaction
pass left behind.
"""

from src import context_health as health
from src.context_ledger import evidence_for_read


def _tool_msg(text):
    return {"role": "tool", "content": text}


def _retrieved_msg(text, source="documents"):
    return {"role": "user", "content": text,
            "metadata": {"trusted": False, "source": source}}


# ── duplicate_ratio ──────────────────────────────────────────────────────────

def test_duplicate_ratio_is_zero_with_no_repeats():
    msgs = [_tool_msg("alpha"), _tool_msg("beta"), _tool_msg("gamma")]
    result = health.duplicate_ratio(msgs)
    assert result["ratio"] == 0.0
    assert result["repeated_count"] == 0


def test_duplicate_ratio_counts_a_repeated_tool_output():
    msgs = [_tool_msg("same content"), _tool_msg("different"),
            _tool_msg("same content"), _tool_msg("same content")]
    result = health.duplicate_ratio(msgs)
    assert result["repeated_count"] == 2  # the 2nd and 3rd occurrence of "same content"
    assert result["counted"] == 4
    assert result["ratio"] == 0.5


def test_duplicate_ratio_ignores_plain_conversation():
    # Two identical user turns are not "reading without progress" — only
    # tool/retrieved content counts (see the module docstring).
    msgs = [{"role": "user", "content": "hello"}, {"role": "user", "content": "hello"}]
    result = health.duplicate_ratio(msgs)
    assert result["counted"] == 0
    assert result["ratio"] == 0.0


# ── stale_evidence ───────────────────────────────────────────────────────────

def test_stale_evidence_flags_a_changed_file():
    ev = evidence_for_read("src/foo.py", 1, 3, "original content")
    msgs = [{"role": "assistant", "content": "read the file",
             "metadata": {"evidence_refs": [ev.to_mapping()]}}]

    def read_file(path):
        assert path == "src/foo.py"
        return "content has since changed"

    result = health.stale_evidence(msgs, read_file=read_file)
    assert result["stale_count"] == 1
    assert result["stale"][0]["evidence_id"] == ev.evidence_id


def test_stale_evidence_is_clean_when_content_still_matches():
    ev = evidence_for_read("src/foo.py", 1, 3, "unchanged content")
    msgs = [{"role": "assistant", "content": "read the file",
             "metadata": {"evidence_refs": [ev.to_mapping()]}}]
    result = health.stale_evidence(msgs, read_file=lambda p: "unchanged content")
    assert result["stale_count"] == 0


def test_stale_evidence_reports_unreadable_file_as_unknown_not_stale():
    ev = evidence_for_read("src/gone.py", 1, 1, "x")
    msgs = [{"role": "assistant", "content": "x",
             "metadata": {"evidence_refs": [ev.to_mapping()]}}]
    result = health.stale_evidence(msgs, read_file=lambda p: None)
    assert result["stale_count"] == 0
    assert len(result["unknown"]) == 1


# ── health() ─────────────────────────────────────────────────────────────────

def test_health_flags_repetition_and_stale_evidence_together():
    ev = evidence_for_read("src/bar.py", 1, 1, "v1")
    msgs = [
        {"role": "user", "content": "what does bar.py do"},
        _tool_msg("reading bar.py: v1 text"),
        _tool_msg("reading bar.py: v1 text"),
        _tool_msg("reading bar.py: v1 text"),
        {"role": "assistant", "content": "here you go",
         "metadata": {"evidence_refs": [ev.to_mapping()]}},
    ]
    report = health.health(msgs, read_file=lambda p: "v2 — changed since")
    keys = {f["key"] for f in report["flags"]}
    assert "repetition" in keys
    assert "stale_evidence" in keys
    assert report["needs_rebuild"] is True


def test_health_is_quiet_on_a_clean_window():
    msgs = [{"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"}]
    report = health.health(msgs, read_file=lambda p: None)
    assert report["flags"] == []
    assert report["needs_rebuild"] is False


# ── reconstruct_task ─────────────────────────────────────────────────────────

def test_reconstruct_task_recovers_goal_constraints_changes_and_next_action():
    msgs = [
        {"role": "system", "content": "You must never delete files without confirmation.\n"
                                       "Always run tests before finishing."},
        {"role": "user", "content": "Fix the bug in src/app.py"},
        {"role": "assistant", "content": "I edited src/app.py and tests/test_app.py",
         "tool_calls": None},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "run_tests", "arguments": "{}"}}]},
    ]
    task = health.reconstruct_task(msgs)
    assert task["goal"] == "Fix the bug in src/app.py"
    assert any("never delete" in c.lower() for c in task["constraints"])
    assert any("always run tests" in c.lower() for c in task["constraints"])
    assert "src/app.py" in task["changes"]
    assert "tests/test_app.py" in task["changes"]
    assert task["next_action"] == "tool: run_tests"


def test_reconstruct_task_survives_after_a_compaction_marker():
    """The acceptance criterion in one line: recover goal/constraints/changes/
    next_action correctly even when the middle of the transcript has been
    folded into a compaction marker — exactly what
    `context_compactor.compact_with_integrity` produces."""
    msgs = [
        {"role": "system", "content": "Never commit secrets to the repo."},
        {"role": "user", "content": "Add caching to src/context_engine/cache.py"},
        {"role": "assistant", "content": "[compactado: 14 mensajes, 3100 tokens -> 220] "
                                          "touched src/context_engine/cache.py"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "edit_file", "arguments": "{}"}}]},
    ]
    task = health.reconstruct_task(msgs)
    assert task["goal"] == "Add caching to src/context_engine/cache.py"
    assert any("secrets" in c.lower() for c in task["constraints"])
    assert "src/context_engine/cache.py" in task["changes"]
    assert task["next_action"] == "tool: edit_file"
