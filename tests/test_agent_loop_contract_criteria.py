"""The agent loop with contract criteria (src/contract_criteria.py, #1127):
driven by a scripted model like tests/test_unattended_conversation.py.

An unattended task with a long statement: the criteria are extracted at the
start, the model links one with `criteria_check`, the completion audit lists
the open ones, ONE `contract_gap` round follows, then the turn ends and the
summary/gate report what stayed open. Off, attended or short: as before."""
import asyncio
import json

import pytest

import src.agent_loop as al
from src.agent_tools import TOOL_HANDLERS

SPEC = (
    "# Archive tool\n\n"
    "Build a small archive tool for the team. " + "It packs project folders for the nightly backup job. " * 12 + "\n\n"
    "## Rules\n\n"
    "- `pack`, `unpack` and `scan` stream: their memory use must stay under 64 MB for any input size.\n"
    "- A damaged archive fails with `corrupt` and exit code 1.\n"
    "- Every command prints exactly one JSON document on stdout.\n"
    "- The tool must never delete the source files.\n\n"
    "## Background\n\n" + "The previous tool was a shell script that nobody maintained any more. " * 12 + "\n"
)


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _text(text, finish="stop"):
    return [
        f'data: {json.dumps({"delta": text})}\n\n',
        f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n',
        "data: [DONE]\n\n",
    ]


def _tool(name, body):
    return _text(f"```{name}\n{body}\n```", "tool_calls")


@pytest.fixture
def loop(monkeypatch, tmp_path):
    # The harness's own linking is off here: these tests drive the model's
    # `criteria_check` path and count its links; auto-linking has its own tests.
    settings = {"agent_contract_criteria_auto_link": False}
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: settings.get(key, default), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr("src.plan_tracker.PLAN_TRACKER_DIR", str(tmp_path / "tracker"))
    executed = []
    options = []

    async def _fake_exec(block, *a, **k):
        executed.append(block.tool_type)
        options.append(dict(k.get("turn_options") or {}))
        if block.tool_type == "criteria_check":
            ctx = {"contract_book": (k.get("turn_options") or {}).get("contract_book")}
            return block.tool_type, await TOOL_HANDLERS["criteria_check"](block.content, ctx)
        if block.tool_type == "bash":
            return block.tool_type, {"output": '{"error": {"code": "corrupt", "message": "crc"}}', "exit_code": 1}
        return block.tool_type, {"output": "src/arc/__init__.py", "exit_code": 0}
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    def run(rounds, *, unattended=True, user=SPEC, max_rounds=12, messages=None):
        seen = []

        async def _fake_stream(_candidates, messages, **kwargs):
            i = min(len(seen), len(rounds) - 1)
            seen.append([dict(m) for m in messages])
            for chunk in rounds[i]:
                yield chunk
        monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
        gen = al.stream_agent_loop(
            "http://127.0.0.1:11434/v1", "qwen3-coder:30b", messages or [{"role": "user", "content": user}],
            max_rounds=max_rounds, relevant_tools={"read_file", "glob", "bash"},
            workspace=str(tmp_path), unattended=unattended,
        )
        return seen, _events(_collect(gen))

    run.settings = settings
    run.executed = executed
    run.options = options
    return run


def _scripted_turn():
    # bash before anything untrusted was read: after that it needs approval.
    return [
        _tool("bash", "python -m arc unpack damaged.arc"),
        _tool("glob", json.dumps({"pattern": "src/**/*.py"})),
        _tool("criteria_check", json.dumps({"links": [{"id": "C04", "command": "unpack damaged.arc"}]})),
        _text("Done: the archive tool is ready."),
        _text("Checked every requirement listed."),
        _text("Final answer: pack/unpack/scan stream; damaged archives fail with corrupt."),
        _text("unexpected extra round"),
    ]


def _summary(events):
    return next(e["data"] for e in reversed(events) if e.get("type") == "harness_summary")


def test_unattended_contract_turn_audits_the_criteria_and_takes_one_gap_round(loop):
    seen, events = loop(_scripted_turn())
    start = [e for e in events if e.get("status") == "contract_criteria" and not e.get("final")]
    assert len(start) == 1 and start[0]["count"] >= 6, events
    assert start[0]["ids"][3] == "C04"
    # the compact list reached the model at the start, once
    first = json.dumps(seen[0], ensure_ascii=False)
    assert "[Contract criteria" in first and "C04 L" in first
    assert "criteria_check" in loop.executed
    # every tool call carries the turn's book and the paths changed so far
    assert all(o.get("contract_book") is not None for o in loop.options)
    assert all(isinstance(o.get("ledger_mutations"), list) for o in loop.options)
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    assert len(audits) == 1 and audits[0]["criteria"] == start[0]["count"] - 1
    audit_text = seen[4][-1]["content"]
    # the generic audit is kept whole; the open criteria are appended to it
    assert audit_text.startswith(al.COMPLETION_AUDIT_NOTE + "\n\n")
    assert "C01" in audit_text and "[unpack]" in audit_text and "C04 L" not in audit_text
    gaps = [e for e in events if e.get("reason") == "contract_gap"]
    assert len(gaps) == 1, [e.get("reason") for e in events if e.get("type") == "harness_check"]
    assert gaps[0]["cause"] == "contract_gap" and gaps[0]["family"] == "completion"
    assert "C04" not in gaps[0]["pending"] and "C01" in gaps[0]["pending"]
    assert "still have no accepted evidence" in seen[5][-1]["content"]
    assert len(seen) == 6, len(seen)
    summary = _summary(events)
    crit = summary["criteria"]
    assert crit["proven"] == 1 and crit["ids"]["proven"] == ["C04"]
    assert crit["pending"] == crit["total"] - 1
    assert summary["stop_reason"] == "complete_unverified"
    assert "evidence_grade:criteria_unverified" in summary["notes"]
    assert any(n.startswith("contract_criteria:") for n in summary["notes"])
    final = [e for e in events if e.get("status") == "contract_criteria" and e.get("final")]
    assert final and final[0]["proven"] == 1


def test_audit_and_gap_round_fire_after_the_round_budget_was_extended(loop):
    # max_rounds is ONE grant (3 here); the turn's real budget grows with every
    # automatic extension. Round 4 is past the first grant, so a gate that still
    # compared with max_rounds would silently skip the audit and the gap round.
    rounds = [
        _tool("bash", "python -m arc unpack damaged.arc"),
        _tool("glob", json.dumps({"pattern": "src/**/*.py"})),
        _tool("glob", json.dumps({"pattern": "tests/**/*.py"})),
        _text("Done: the archive tool is ready."),
        _text("Checked every requirement listed."),
        _text("Final answer."),
        _text("unexpected extra round"),
    ]
    seen, events = loop(rounds, max_rounds=3)
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    gaps = [e for e in events if e.get("reason") == "contract_gap"]
    assert len(audits) == 1 and audits[0]["round"] == 4 and audits[0]["criteria"] == 6, events
    assert len(gaps) == 1 and gaps[0]["round"] == 5, [e.get("reason") for e in events if e.get("type") == "harness_check"]
    assert seen[4][-1]["content"].startswith(al.COMPLETION_AUDIT_NOTE)
    assert "still have no accepted evidence" in seen[5][-1]["content"]
    notes = _summary(events)["notes"]
    assert "completion_audit@4" in notes and "contract_gap@5" in notes
    assert any(n.startswith("auto_continue_rounds@") for n in notes), notes


def test_no_gate_of_the_loop_compares_the_round_with_the_first_grant():
    # `max_rounds` never changes; `_rounds_budget` is the turn's real budget
    # (src/agent_loop.py). A comparison with the grant is false after the first
    # automatic extension and turns the guarded recovery paths off.
    import re
    from pathlib import Path
    source = Path(al.__file__).read_text(encoding="utf-8")
    stale = re.findall(r"round_num\s*(?:<=?|>=?|==)\s*max_rounds\b|\bmax_rounds\s*(?:<=?|>=?)\s*round_num\b", source)
    assert stale == [], f"gate(s) compare the round with the first grant: {stale}"
    assert "max_rounds=max_rounds" not in source, "TextRoundState must get the real budget"
    assert "max_rounds=_rounds_budget" in source


def test_the_harness_links_a_fresh_run_by_itself_and_the_model_keeps_the_rest(loop):
    loop.settings["agent_contract_criteria_auto_link"] = True
    seen, events = loop(_scripted_turn())
    crit = _summary(events)["criteria"]
    # the bash run shows `corrupt` / exit 1: no criteria_check needed for C04. The bare
    # "one JSON document" rule is not the harness's to prove from just any JSON output.
    assert "C04" in crit["ids"]["proven"] and "C05" not in crit["ids"]["proven"]
    assert {"C01", "C05", "C06"} <= set(crit["ids"]["pending"])
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    assert len(audits) == 1 and audits[0]["criteria"] == 5
    assert "C04 L" not in seen[4][-1]["content"] and "C01" in seen[4][-1]["content"]


def test_the_start_note_is_a_user_role_harness_note_the_trimmer_keeps(loop):
    seen, _events = loop(_scripted_turn())
    notes = [m for m in seen[0] if "[Contract criteria" in str(m.get("content"))]
    assert len(notes) == 1
    note = notes[0]
    assert note["role"] == "user" and note.get("_harness_note") is True
    assert (note.get("metadata") or {}).get("contract_criteria") is True
    # an id taken from the book, no deferred "before you finish", no stray brace
    import re
    example = re.search(r'"id": "(C\d+)"', note["content"])
    assert example and example.group(1) in {f"C{i:02d}" for i in range(1, 7)}
    assert "Before you finish" not in note["content"]
    assert "}}" not in note["content"].split("more open")[-1]
    # it sits in front of the person's own request, not in the system block
    idx = seen[0].index(note)
    task = next(i for i, m in enumerate(seen[0]) if m.get("content") == SPEC)
    assert idx < task and all(m["role"] == "user" for m in seen[0][idx:task])
    from src.context_compactor import is_pinned_note
    assert is_pinned_note(note)
    assert not [m for m in seen[0] if m.get("role") == "system" and "[Contract criteria" in str(m.get("content"))]


def test_mass_not_applicable_keeps_the_audit_the_gap_round_and_the_gate(loop):
    links = [{"id": f"C{i:02d}", "status": "not_applicable", "reason": "covered by design"} for i in range(1, 30)]
    rounds = [
        _tool("glob", json.dumps({"pattern": "*.py"})),
        _tool("criteria_check", json.dumps({"links": links})),
        _text("Done."),
        _text("Checked."),
        _text("Final."),
        _text("unexpected extra round"),
    ]
    seen, events = loop(rounds)
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    assert len(audits) == 1 and audits[0]["criteria"] == 6
    assert "Claimed not applicable" in seen[3][-1]["content"]
    gaps = [e for e in events if e.get("reason") == "contract_gap"]
    assert len(gaps) == 1 and gaps[0]["claimed_not_applicable"] == [f"C{i:02d}" for i in range(1, 7)]
    assert len(seen) == 5
    summary = _summary(events)
    assert summary["criteria"]["mass_not_applicable"] and summary["criteria"]["open_total"] == 6
    assert summary["stop_reason"] == "complete_unverified"


def test_no_gap_round_when_every_criterion_is_settled(loop):
    # lenient evidence: any real call of the turn proves; one not_applicable
    # (under a quarter) is accepted as settled.
    loop.settings["agent_contract_criteria_strict_evidence"] = False
    links = [{"id": f"C{i:02d}", "command": "unpack damaged"} for i in range(1, 6)]
    links.append({"id": "C06", "status": "not_applicable", "reason": "the tool never touches sources"})
    rounds = [
        _tool("bash", "python -m arc unpack damaged.arc"),
        _tool("criteria_check", json.dumps({"links": links})),
        _text("Done: the archive tool is ready."),
        _text("Checked every requirement listed."),
        _text("unexpected extra round"),
    ]
    seen, events = loop(rounds)
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    assert len(audits) == 1 and "criteria" not in audits[0]
    assert seen[3][-1]["content"] == al.COMPLETION_AUDIT_NOTE
    assert not [e for e in events if e.get("reason") == "contract_gap"]
    assert len(seen) == 4
    summary = _summary(events)
    assert summary["criteria"]["open_total"] == 0 and summary["criteria"]["proven"] == 5
    assert summary["stop_reason"] == "complete"


def test_gap_rounds_setting_zero_reports_only(loop):
    loop.settings["agent_contract_criteria_extra_rounds"] = 0
    seen, events = loop(_scripted_turn())
    assert len([e for e in events if e.get("reason") == "completion_audit"]) == 1
    assert not [e for e in events if e.get("reason") == "contract_gap"]
    assert len(seen) == 5
    assert _summary(events)["criteria"]["pending"] > 0


def test_gap_rounds_setting_is_clamped(loop):
    loop.settings["agent_contract_criteria_extra_rounds"] = 50
    rounds = _scripted_turn()[:5] + [_text("again")] * 8
    seen, events = loop(rounds, max_rounds=20)
    gaps = [e for e in events if e.get("reason") == "contract_gap"]
    assert len(gaps) == 3 and gaps[-1]["budget_limit"] == 3


def test_setting_off_keeps_the_generic_audit_and_no_criteria(loop):
    loop.settings["agent_contract_criteria"] = "off"
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("Checked."),
              _text("unexpected extra round")]
    seen, events = loop(rounds)
    assert not [e for e in events if e.get("status") == "contract_criteria" or e.get("reason") == "contract_gap"]
    assert seen[2][-1]["content"] == al.COMPLETION_AUDIT_NOTE
    assert "[Contract criteria" not in json.dumps(seen[0])
    assert len(seen) == 3
    assert "criteria" not in _summary(events)


def test_short_message_changes_nothing(loop):
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("Checked."),
              _text("unexpected extra round")]
    seen, events = loop(rounds, user="Make /app/solve.py pass eval.py. It must exit 0. It must never print a trace.")
    assert not [e for e in events if e.get("status") == "contract_criteria"]
    assert seen[2][-1]["content"] == al.COMPLETION_AUDIT_NOTE
    assert len(seen) == 3


def test_extraction_over_its_time_budget_skips_the_feature_for_the_turn(loop, monkeypatch):
    import time as _time
    from src import contract_criteria as cc

    def _slow(*a, **k):
        _time.sleep(0.6)
        return None
    monkeypatch.setattr(cc, "TIME_BUDGET_S", 0.05)
    monkeypatch.setattr(cc, "build_for_turn", _slow)
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("Checked."),
              _text("unexpected extra round")]
    seen, events = loop(rounds)
    skipped = [e for e in events if e.get("status") == "contract_criteria"]
    assert len(skipped) == 1 and skipped[0]["skipped"] == "timeout"
    assert seen[2][-1]["content"] == al.COMPLETION_AUDIT_NOTE
    assert "contract_criteria:skipped_timeout" in _summary(events)["notes"]


def test_the_latest_message_is_read_even_with_list_content(loop):
    old = SPEC.replace("archive tool", "legacy tool").replace("`corrupt`", "`broken`")
    msgs = [{"role": "user", "content": old}, {"role": "assistant", "content": "Done."},
            {"role": "user", "content": [{"type": "text", "text": SPEC}]}]
    seen, events = loop(_scripted_turn(), messages=msgs)
    first = json.dumps(seen[0], ensure_ascii=False)
    assert "`corrupt`" in first.split("[Contract criteria", 1)[1][:3000]
    assert _summary(events)["criteria"]["ids"]["proven"] == ["C04"]


def test_attended_turn_extracts_with_always_but_takes_no_extra_round_and_no_gate(loop):
    loop.settings["agent_contract_criteria"] = "always"
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("unexpected extra round")]
    seen, events = loop(rounds, unattended=False)
    assert [e for e in events if e.get("status") == "contract_criteria" and not e.get("final")]
    assert not [e for e in events if e.get("reason") in ("completion_audit", "contract_gap")]
    assert "[Contract criteria" not in json.dumps(seen[0])
    assert len(seen) == 2
    summary = _summary(events)
    assert summary["criteria"]["open_total"] == 6 and not summary["criteria"]["shown"]
    assert "evidence_grade:criteria_unverified" not in summary["notes"]


def test_attended_turn_with_the_default_mode_is_untouched(loop):
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("unexpected extra round")]
    seen, events = loop(rounds, unattended=False)
    assert not [e for e in events if e.get("status") == "contract_criteria"]
    assert len(seen) == 2


def test_a_continue_turn_resumes_the_book_but_proven_must_be_earned_again(loop):
    loop.settings["agent_contract_criteria_extra_rounds"] = 0
    first_seen, first = loop(_scripted_turn())
    assert _summary(first)["criteria"]["ids"]["proven"] == ["C04"]
    rounds = [_tool("glob", json.dumps({"pattern": "*.py"})), _text("Done."), _text("Checked."),
              _text("unexpected extra round")]
    seen, events = loop(rounds, user="continúa")
    start = [e for e in events if e.get("status") == "contract_criteria" and not e.get("final")]
    assert len(start) == 1 and start[0]["resumed"] and start[0]["open"] == start[0]["count"]
    crit = _summary(events)["criteria"]
    assert crit["ids"]["proven"] == [] and "C04" in crit["ids"]["pending"]


@pytest.mark.parametrize("browser_alias", [
    "mcp__browser_session__browser_session", "mcp__823416aa__browser_session",
])
def test_remote_browser_progress_is_not_a_repository_deliverable(browser_alias, tmp_path):
    from src.agent_harness import TurnLedger
    from tests.test_contract_criteria import _R4_OPERATIONAL_1797

    ledger = TurnLedger(str(tmp_path), "Observa el navegador y devuelve el contador; sin archivos.")
    packet = _R4_OPERATIONAL_1797[0]
    event = ledger.record(browser_alias, packet["content"],
                          {"output": packet["output"], "exit_code": 0}, 1)
    progress = ledger.record_progress([{"content": "Leer contador", "status": "completed"}], 2)
    assert event["ok"] and event["kind"] == "read"
    assert progress[0]["verified"] is True
    assert ledger.mutations == [] and ledger.effects == []
    # Existing valid controls before the new remote-progress assertion.
    assert al._workspace_completion_work({"workspace_work": True}, [], ledger) is True
    assert al._workspace_completion_work({"workspace_work": False}, ["report.txt"], ledger) is True
    changed = TurnLedger(str(tmp_path), "Escribe report.txt.")
    changed.record("edit_file", '{"path":"report.txt"}',
                   {"output": "Edited report.txt", "exit_code": 0}, 3)
    assert al._workspace_completion_work({"workspace_work": False}, [], changed) is True
    assert al._workspace_completion_work({"workspace_work": False}, [], ledger) is False


def test_failed_browser_read_remains_a_failure_and_does_not_accredit_progress(tmp_path):
    from src.agent_harness import TurnLedger

    ledger = TurnLedger(str(tmp_path), "Observa el navegador.")
    ledger.record("mcp__browser_session__browser_session", '{"action":"snapshot"}',
                  {"error": "document_changed", "exit_code": 1}, 1)
    progress = ledger.record_progress([{"content": "Leer contador", "status": "completed"}], 2)
    assert progress[0]["verified"] is False
    assert ledger.summary()["failed_calls"] == 1
    assert ledger.failed[0]["error"] == "document_changed"


def test_local_audit_and_gap_keep_history_but_replace_the_final_answer(loop, monkeypatch):
    import importlib.util
    from pathlib import Path
    import httpx
    import requests
    from src import model_context, run_model_pin

    attempted_http = []

    def no_http(*args, **kwargs):
        attempted_http.append(True)
        raise AssertionError("CPU fixture must not perform HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    monkeypatch.setattr(requests.Session, "request", no_http)
    monkeypatch.setattr(model_context, "_query_context_length", lambda *a, **k: (131072, True))
    monkeypatch.setattr(run_model_pin, "restore_keep_alive", lambda *a, **k: None)

    drafts = ("First archive observation.", "Revised archive observation.", "Final archive observation.")
    seen, events = loop([
        _tool("glob", json.dumps({"pattern": "src/**/*.py"})),
        _text(drafts[0]), _text(drafts[1]), _text(drafts[2]),
    ])
    assert len(seen) == 4
    assert len([e for e in events if e.get("reason") == "completion_audit"]) == 1
    assert len([e for e in events if e.get("reason") == "contract_gap"]) == 1
    assert _summary(events)["criteria"]["pending"] > 0
    # Test the incumbent CLI accumulator too; no server, CLI main or model runs.
    path = Path(al.__file__).resolve().parents[1] / "scripts" / "faustus_run.py"
    spec = importlib.util.spec_from_file_location("faustus_run1797", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    turn = cli.Turn()
    for event in events:
        turn.feed(event)
    assert drafts[2] in turn.text
    assert drafts[0] not in turn.text and drafts[1] not in turn.text
    replacements = [e for e in events if e.get("type") == "response_replace"]
    assert len(replacements) >= 2
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    history = "\n".join(metrics["round_texts"])
    assert all(draft in history for draft in drafts)
    assert attempted_http == []


_BROWSER_OBSERVATION_1797 = (
    "Comprobación breve de interfaz, sin entregable de código ni artefacto local.\n"
    "No modificar archivos, crear planes de implementación ni retomar pendientes locales.\n"
    "Usa sólo el navegador compartido incorporado mcp__browser_session__browser_session "
    "para observar y actuar sobre la interfaz.\n"
    "No shell, escritorio, otro navegador, lecturas de código o artefactos.\n"
    "- Los IDs de pestañas actuales deben conservarse.\n"
    "- Elige la pestaña QA únicamente si su URL empieza por http://127.0.0.1:63395/.\n"
    "- Abre ese proyecto y lee el botón Biblioteca.\n"
    "- El contador debe obtenerse literalmente antes de volver a Inicio.\n"
    "- Los seis proyectos deben comprobarse sin editar datos.\n"
    "- El resultado debe confirmar release y ser breve.\n"
    "Si el servidor o la extensión no están disponibles, conserva el error y termina.\n"
)


@pytest.mark.parametrize("extra,local", [
    ("", False),
    ("Además modifica src/editor.ts y ejecuta sus pruebas.", True),
    ("Además escribe informe.xlsx con el resultado.", True),
    ("Además revisa el código del repositorio.", True),
    ("Además lee src/editor.ts y explica su comportamiento.", True),
    ("No edites datos; pero además modifica src/editor.ts.", True),
    ("No crear archivos, pero escribe informe.xlsx con el resultado.", True),
    ("Además revisa el diff.", True),
    ("Además ejecuta pytest.", True),
    ("Además lee src/main.go y explica su comportamiento.", True),
    ("Además ejecuta npm test.", True),
    ("Después ejecuta pytest.", True),
    ("Además lee el contador de la Biblioteca.", False),
    ("La vista añade context_nodes con sus padres originales.", False),
    ("Después lee src/parse_count.go y explica su comportamiento.", True),
    ("Además revisa la función parse_count(value).", True),
])
def test_explicit_browser_scope_preserves_positive_local_requests(extra, local):
    text = _BROWSER_OBSERVATION_1797 + extra
    intent = al._classify_agent_request([{"role": "user", "content": text}], text)
    assert intent["workspace_work"] is local


@pytest.mark.parametrize("mode", ["browser", "browser_failure", "code", "file", "local_read"])
def test_browser_only_loop_keeps_advisory_criteria_without_local_closing_discipline(
        tmp_path, monkeypatch, mode):
    import httpx
    import requests
    from src import model_context, run_model_pin, tool_utils
    from src.browser_relay.protocol import SCHEMA
    from src.mcp_manager import McpManager
    from tests.test_agent_harness_functional import _patch_common

    attempts = []
    def no_http(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("CPU fixture must not perform HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    monkeypatch.setattr(requests.Session, "request", no_http)
    monkeypatch.setattr(model_context, "_query_context_length", lambda *a, **k: (131072, True))
    monkeypatch.setattr(run_model_pin, "restore_keep_alive", lambda *a, **k: None)
    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: str(tmp_path))
    _patch_common(monkeypatch, settings={
        "agent_project_tests": False, "agent_ui_smoke": False, "agent_auto_review": "off",
        "agent_project_todos": False, "agent_contract_criteria_min_chars": 0,
        "agent_contract_criteria_auto_link": False, "agent_plan_autocontinue": False,
    })
    # Populate the ordinary discovery cache with the shipping Browser schema.
    # The real manager qualifies, advertises and filters it; no phantom alias
    # or bypass of the loop's normal MCP catalogue is involved.
    manager = McpManager()
    manager._tools["browser_session"] = [{
        "name": "browser_session", "description": "Use the existing browser session.",
        "input_schema": SCHEMA,
    }]
    manager._connections["browser_session"] = {"name": "Browser Session", "transport": "stdio"}
    monkeypatch.setattr(al, "get_mcp_manager", lambda: manager)
    monkeypatch.setattr(tool_utils, "_mcp_manager", manager)
    alias = "mcp__browser_session__browser_session"
    assert alias in {s["function"]["name"] for s in manager.get_all_openai_schemas()}
    executed = []

    async def execute(block, *args, **kwargs):
        executed.append((block.tool_type, block.content))
        if block.tool_type == alias:
            action = json.loads(block.content)["action"]
            if mode == "browser_failure" and action == "snapshot":
                return alias, {"output": json.dumps({"ok": False, "error": {
                    "code": "document_changed", "phase": "not_dispatched"}}), "exit_code": 1}
            result = ({"released": True} if action == "release" else
                      {"tabs": [{"id": 7, "active": True, "url": "http://127.0.0.1:63395/"}]} if action == "tabs" else
                      {"nodes": [{"ref": "p1", "role": "StaticText", "name": "36 visibles de 36"}]})
            return alias, {"output": json.dumps({"ok": True, "result": result}), "exit_code": 0}
        if block.tool_type == "read_file":
            return "read_file", {"output": "const count = 36;", "exit_code": 0}
        raise AssertionError("Unexpected tool: " + block.tool_type)

    monkeypatch.setattr(al, "execute_tool_block", execute)
    suffix = {"code": "Además modifica src/editor.ts y ejecuta sus pruebas.",
              "file": "Además escribe informe.xlsx con el resultado."}.get(mode, "")
    user = _BROWSER_OBSERVATION_1797 + suffix
    commands = [{"action": "tabs"}, {"action": "snapshot", "tab_id": 7},
                {"action": "tabs"}, {"action": "release", "tab_id": 7}]
    rounds = [[{"name": alias, "arguments": json.dumps(c)}] for c in commands]
    if mode == "local_read":
        rounds.append([{"name": "read_file", "arguments": '{"path":"src/editor.ts"}'}])
    rounds.extend(["Observación terminada.", "Comprobación local pendiente.",
                   "Resultado final; quedan requisitos sin acreditar."])
    calls = {"n": 0, "messages": []}

    async def scripted_stream(_candidates, messages, **kwargs):
        item = rounds[min(calls["n"], len(rounds) - 1)]
        calls["n"] += 1
        calls["messages"].append([dict(m) for m in messages])
        packet = {"delta": item} if isinstance(item, str) else {"type": "tool_calls", "calls": item}
        yield "data: " + json.dumps(packet) + "\n\n"
        yield "data: " + json.dumps({"type": "finish", "finish_reason":
                                    "stop" if isinstance(item, str) else "tool_calls"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", scripted_stream)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b", [{"role": "user", "content": user}],
        relevant_tools={alias, "read_file", "todowrite"}, workspace=str(tmp_path),
        session_id="browser-scope1797-" + mode, unattended=True,
        # Same explicit full-access task mode as the authorized native run;
        # trusted_workspace alone covers files, not unknown MCP capabilities.
        security_gate_bypass=True,
        harness_options={"trusted_workspace": str(tmp_path)}, max_rounds=9,
    )))
    summary = _summary(events)
    assert [t for t, _ in executed] == [alias] * 4 + (["read_file"] if mode == "local_read" else [])
    assert attempts == [] and summary["mutations"] == [] and summary["effects"] == 0
    assert summary["failed_calls"] == int(mode == "browser_failure")
    assert json.loads(executed[3][1])["action"] == "release"
    assert summary["criteria"]["pending"] > 0 and not summary["criteria"]["ids"]["proven"]
    notes = "\n".join(m.get("content", "") for batch in calls["messages"] for m in batch
                      if m.get("_harness_note"))
    reasons = [e.get("reason") for e in events if e.get("type") == "harness_check"]
    if mode in {"browser", "browser_failure"}:
        assert calls["n"] == 5
        assert "completion_audit" not in reasons and "contract_gap" not in reasons
        assert "[Contract criteria" not in notes and "Call todowrite ONCE" not in notes
        assert not summary["criteria"]["shown"]
    else:
        assert reasons.count("completion_audit") == 1 and reasons.count("contract_gap") == 1
        assert calls["n"] == (8 if mode == "local_read" else 7)
        if mode != "local_read":
            assert "[Contract criteria" in notes and "Call todowrite ONCE" in notes
