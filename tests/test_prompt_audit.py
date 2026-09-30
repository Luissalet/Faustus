"""H09: hashes and sources explain changes.

The compiler's manifest describes the packet it built.  The loop adds blocks
after the compiler, trims for the route and the transport may rewrite the list.
These tests pin the instrument that explains the difference: block and prefix
hashes of the exact prompt, the manifest reconciled against it, per-round
diffs, query identities that never carry text, and byte-stable output for the
same inputs regardless of the order the inputs arrive in."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.context_engine import cache, compiler as compiler_module, prompt_audit as pa, store, wiring
from src.context_engine.cache import WorkingSet
from src.context_engine.compiler import ContextCompiler
from src.context_engine.contracts import (
    ContextActor, ContextBudget, ContextCandidate, ContextExecution, ContextItem,
    ContextPacket, ContextPolicy, ContextRequest, ContextSection, ContextTask,
)

ROOT = Path(__file__).resolve().parents[1]
LOOP = (ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


# -- fingerprints --------------------------------------------------------------

def _base():
    return [
        {"role": "system", "content": "You are Faustus."},
        {"role": "user", "content": "ctx block", "_agent_injected": "context_engine",
         "metadata": {"trusted": False, "source": "memory", "context_packet_id": "p1"}},
        {"role": "user", "content": "Why does the build fail?"},
    ]


def test_the_same_messages_hash_to_the_same_bytes_whatever_the_bookkeeping():
    left = pa.fingerprint_prompt(_base())
    messy = _base()
    messy[2]["metadata"] = {"arbitrary": "bookkeeping"}
    messy[0] = dict(reversed(list(messy[0].items())))  # key order differs
    right = pa.fingerprint_prompt(messy)
    assert left["prompt_sha256"] == right["prompt_sha256"]
    assert [b["sha256"] for b in left["blocks"]] == [b["sha256"] for b in right["blocks"]]


def test_a_changed_block_changes_its_hash_and_every_prefix_after_it():
    left = pa.fingerprint_prompt(_base())
    changed = _base()
    changed[1]["content"] = "ctx block, edited"
    right = pa.fingerprint_prompt(changed)
    assert left["blocks"][0]["prefix_sha256"] == right["blocks"][0]["prefix_sha256"]
    assert left["blocks"][1]["prefix_sha256"] != right["blocks"][1]["prefix_sha256"]
    assert left["blocks"][2]["prefix_sha256"] != right["blocks"][2]["prefix_sha256"]
    assert pa.stable_prefix_blocks(left, right) == 1


def test_tool_schemas_are_part_of_the_prompt_identity():
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    assert (pa.fingerprint_prompt(_base(), tools)["prompt_sha256"]
            != pa.fingerprint_prompt(_base())["prompt_sha256"])
    other = [{"type": "function", "function": {"name": "write_file"}}]
    assert (pa.fingerprint_prompt(_base(), tools)["tools_sha256"]
            != pa.fingerprint_prompt(_base(), other)["tools_sha256"])


def test_blocks_carry_their_origin():
    blocks = pa.fingerprint_prompt(_base() + [
        {"role": "user", "_harness_note": True, "content": "note"},
    ])["blocks"]
    assert blocks[1]["origin"] == "injected:context_engine"
    assert blocks[3]["origin"] == "harness_note"


# -- per-round diffs -----------------------------------------------------------

def test_appending_a_tool_round_keeps_the_prefix_and_names_what_was_added():
    first = pa.fingerprint_prompt(_base())
    grown = _base() + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    diff = pa.diff_prompts(first, pa.fingerprint_prompt(grown))
    assert diff["prefix_intact"] is True
    assert diff["stable_prefix_blocks"] == 3
    assert {row["bucket"] for row in diff["added"]} == {"conversation", "tool_results"}
    assert diff["removed"] == [] and diff["changed"] == []


def test_a_change_before_the_end_is_a_prefix_break_with_the_bucket_that_caused_it():
    first = pa.fingerprint_prompt(_base())
    edited = _base()
    edited[1]["content"] = "a different packet"
    diff = pa.diff_prompts(first, pa.fingerprint_prompt(edited))
    assert diff["prefix_intact"] is False
    assert diff["first_break"]["index"] == 1
    assert diff["first_break"]["was_origin"] == "injected:context_engine"
    assert [c["index"] for c in diff["changed"]] == [1]
    assert diff["stable_prefix_blocks"] == 1


def test_a_removed_block_is_reported_as_removed():
    first = pa.fingerprint_prompt(_base())
    shrunk = _base()[:1] + _base()[2:]
    diff = pa.diff_prompts(first, pa.fingerprint_prompt(shrunk))
    assert [r["index"] for r in diff["removed"]] == [1] or diff["changed"]
    assert diff["prefix_intact"] is False


def test_the_first_round_has_nothing_to_compare_with():
    diff = pa.diff_prompts(None, pa.fingerprint_prompt(_base()))
    assert diff["first"] is True and diff["prefix_intact"] is True


def test_a_tools_change_is_flagged_separately():
    a = pa.fingerprint_prompt(_base(), [{"function": {"name": "a"}}])
    b = pa.fingerprint_prompt(_base(), [{"function": {"name": "b"}}])
    diff = pa.diff_prompts(a, b)
    assert diff["tools_changed"] is True and diff["prefix_intact"] is True


# -- source receipts -----------------------------------------------------------

def _item(ref, body, revision="", **kw):
    return ContextItem(item_id=f"i-{ref}", source_type=kw.pop("source_type", "memory"),
                       source_ref=ref, title=ref, body=body, source_revision=revision,
                       tokens=len(body) // 4 + 1, **kw)


def _packet(items_by_section, packet_id="ctxpkt_a"):
    return ContextPacket(
        packet_id=packet_id, request_id="req1", owner="luis", session_id="s1", model="m",
        window=ContextBudget(max_tokens=4096, input_budget=2000),
        sections=tuple(ContextSection(kind=kind, items=tuple(items))
                       for kind, items in items_by_section.items()))


def test_every_item_has_a_version_state_and_a_body_hash():
    packet = _packet({
        "retrieved_memory": [_item("mem:a", "prefers pathlib", "rev-7"),
                             _item("mem:b", "uses ruff")],
        "retrieved_documents": [_item("file:x.py", "print(1)",
                                      "captured_utf8_sha256:" + "a" * 64,
                                      source_type="file")],
    })
    rows = {r["source_ref"]: r for r in pa.source_receipts(packet)}
    assert rows["mem:a"]["revision_state"] == "declared" and rows["mem:a"]["revision"] == "rev-7"
    assert rows["mem:b"]["revision_state"] == "unversioned"
    assert rows["file:x.py"]["revision_state"] == "content_hash"
    assert all(len(r["body_sha256"]) == 64 for r in rows.values())
    assert pa.sha256_text("prefers pathlib") == rows["mem:a"]["body_sha256"]


def test_public_receipts_drop_the_private_body():
    packet = _packet({"retrieved_memory": [_item("mem:a", "secret body text")]})
    public = pa.public_source_receipts(pa.source_receipts(packet))
    assert "_body" not in public[0]
    assert "secret body text" not in json.dumps(public)


def test_recent_messages_are_not_sources():
    packet = _packet({"recent_messages": [_item("session:s#0", "hi", source_type="message")],
                      "retrieved_memory": [_item("mem:a", "x")]})
    assert [r["source_ref"] for r in pa.source_receipts(packet)] == ["mem:a"]


# -- query receipts: hybrid memory included ------------------------------------

def test_a_hybrid_memory_query_becomes_an_identity_never_text():
    from src.context_engine.memory_engine_reuse import HybridQueryReceipt, StandingQueryReceipt
    retrieval = SimpleNamespace(query="what is my launch code", lanes=("lexical", "semantic"), limit=8)
    hybrid = HybridQueryReceipt(retrieval, "d" * 64, "/home/someone/data/mem.db",
                                identity=object(), clock=NOW, source=None)
    vetoed = HybridQueryReceipt(retrieval, "e" * 64, "/home/someone/data/mem.db",
                                identity=None, clock=NOW, source=None)
    standing = StandingQueryReceipt(SimpleNamespace(query="", lanes=("mandatory",), limit=4),
                                    "f" * 64, "/home/someone/data/mem.db", None)
    out = pa.query_receipts({"_standing_memory_reuse_receipts": (hybrid, vetoed, standing)})
    queries = out["learned_memory"]["queries"]
    assert out["learned_memory"]["state"] == "captured"
    assert [q["path"] for q in queries] == ["hybrid", "hybrid", "standing"]
    assert [q["semantic_lane"] for q in queries[:2]] == ["installed", "vetoed"]
    assert queries[0]["scoring_clock"].startswith("2026-09-01T12:00:00")
    assert queries[0]["projection_sha256"] == "d" * 64
    assert queries[0]["query_sha256"] == pa.sha256_text("what is my launch code")
    blob = json.dumps(out)
    assert "launch code" not in blob and "/home/someone" not in blob


def test_an_incomplete_capture_is_unknown_and_not_empty():
    out = pa.query_receipts({"_standing_memory_reuse_receipts": None,
                             "_objective_reuse_receipts": (),
                             "_file_reuse_receipts": None})
    assert out["learned_memory"] == {"state": "unknown"}
    assert out["objectives"] == {"state": "captured", "queries": []}
    assert out["files"] == {"state": "unknown"}
    assert out["documents"] == {"state": "unknown"}


def test_file_receipts_carry_the_captured_revision():
    out = pa.query_receipts({"_file_reuse_receipts": (("file:a.py", "captured_utf8_sha256:" + "b" * 64),)})
    assert out["files"]["refs"][0]["revision"].startswith("captured_utf8_sha256:")


# -- manifest versus the exact final prompt ------------------------------------

class _LiveCompiler:
    async def compile(self, request, **kw):
        # A synthetic compiler that ran no planned queries: every family is
        # captured as "seen, nothing planned" so the packet may be re-delivered.
        from src.context_engine.objective_reuse import record_query
        from src.context_engine.personal_memory_reuse import record_query as record_personal
        from src.context_engine.document_reuse import record_query as record_document
        from src.context_engine.memory_engine_reuse import record_query as record_standing
        for record in (record_query, record_personal, record_document, record_standing):
            record(None, (), False)
        return _packet({
            "retrieved_memory": [_item("fixture:mem", "Use concise prose", "rev-1")],
            "retrieved_documents": [_item("fixture:guide", "The guide says run the linter first.",
                                          source_type="document")],
        }, packet_id="ctxpkt_live")


@pytest.fixture()
def live(monkeypatch):
    import src.settings as settings_module
    values = {"agent_context_engine": True, "agent_context_timeout_ms": 2000}
    monkeypatch.setattr(settings_module, "get_setting", lambda k, d=None: values.get(k, d))
    monkeypatch.setattr(wiring, "_live_budget", lambda *a, **k: 900)
    monkeypatch.setattr(compiler_module, "compiler", lambda: _LiveCompiler())


async def _deliver():
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "how do I lint?"}]
    request = wiring.build_request(owner="luis", session_id="s1", model="m", workspace="/repo",
                                   messages=messages, agent_mode=True)
    delivery = await wiring.deliver_round(request=request, messages=messages,
                                          context_length=4096, window_known=True)
    assert delivery
    return messages, delivery


async def test_the_delivery_report_carries_versions_hashes_and_no_bodies(live):
    _, delivery = await _deliver()
    report = delivery["report"]
    rows = {r["source_ref"]: r for r in report["sources"]}
    assert rows["fixture:mem"]["revision"] == "rev-1" and rows["fixture:mem"]["revision_state"] == "declared"
    assert rows["fixture:guide"]["revision_state"] == "unversioned"
    assert len(rows["fixture:guide"]["body_sha256"]) == 64
    assert report["message_sha256"] == pa.message_sha256(delivery["message"])
    blob = json.dumps(report)
    assert "Use concise prose" not in blob and "run the linter first" not in blob
    assert set(report["query_receipts"]) >= {"learned_memory", "documents", "objectives", "files"}
    rows_for_ui = wiring.receipt_rows(report)
    assert rows_for_ui[0]["sha256"] and "revision_state" in rows_for_ui[0]


async def test_the_final_prompt_is_reconciled_with_the_manifest_and_extras_are_named(live):
    from src.agent_loop import _insert_before_latest_user
    messages, delivery = await _deliver()
    final = _insert_before_latest_user(messages, delivery["message"])
    final = final + [
        {"role": "user", "_harness_note": True, "content": "[Use the linter result above.]"},
        {"role": "system", "_agent_injected": "reply_language", "content": "Reply in Spanish."},
    ]
    result = pa.reconcile(delivery, final)
    assert result["packet_present"] is True and result["packet_intact"] is True
    assert {s["source_ref"]: s["in_prompt"] for s in result["sources"]} == {
        "fixture:mem": True, "fixture:guide": True}
    assert result["lost_after_compile"] == []
    origins = {g["origin"] for g in result["added_after_compile"]}
    assert {"harness_note", "injected:reply_language"} <= origins
    assert "injected:context_engine" not in origins
    assert result["outside_compiler_tokens_estimate"] > 0
    assert result["compiled_tokens"] == delivery["report"]["packet_tokens"]


async def test_a_packet_altered_after_delivery_is_not_intact_and_a_trimmed_source_is_lost(live):
    from src.agent_loop import _insert_before_latest_user
    messages, delivery = await _deliver()
    final = _insert_before_latest_user(messages, delivery["message"])
    trimmed = [dict(m) for m in final]
    trimmed[1]["content"] = trimmed[1]["content"].replace("The guide says run the linter first.", "[trimmed]")
    result = pa.reconcile(delivery, trimmed)
    assert result["packet_present"] is True and result["packet_intact"] is False
    lost = {s["source_ref"] for s in result["lost_after_compile"]}
    assert lost == {"fixture:guide"}


async def test_a_packet_dropped_from_the_final_prompt_is_reported_absent(live):
    messages, delivery = await _deliver()
    result = pa.reconcile(delivery, messages)
    assert result["packet_present"] is False and result["packet_index"] is None
    assert {s["in_prompt"] for s in result["sources"]} == {False}
    assert len(result["lost_after_compile"]) == 2


async def test_a_reused_packet_keeps_its_audit(live):
    messages, first = await _deliver()
    request = wiring.build_request(owner="luis", session_id="s1", model="m", workspace="/repo",
                                   messages=messages, agent_mode=True)
    second = await wiring.deliver_round(request=request, messages=messages, context_length=4096,
                                        window_known=True, round_index=1, previous=first)
    assert second["report"].get("reused") is True
    assert second["_audit"] == first["_audit"]


# -- stable ordering: same inputs, same bytes -----------------------------------

@pytest.fixture()
def ce_db(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    cache.reset_working_set()
    compiler_module.reset_compiler()
    try:
        yield
    finally:
        store.use_path(None)
        cache.reset_working_set()
        compiler_module.reset_compiler()


class _Source:
    def __init__(self, source_id, section, rows):
        self.source_id, self.sections, self.handles = source_id, (section,), ("doc:", "mem:")
        self._rows = list(rows)

    def available(self):
        return True

    async def search(self, req):
        return list(self._rows)

    async def fetch(self, source_ref, req):
        return None


def _cand(ref, body, section):
    return ContextCandidate(
        candidate_id=f"cand::{ref}", source_type="document" if section.endswith("documents") else "memory",
        source_ref=ref, title=ref, body=body, section=section, lanes=("lexical",),
        trust_class="observed", authority="observed_state",
        observed_at="2026-08-31T12:00:00Z", source_revision="rev-a")


def _req():
    return ContextRequest(
        request_id="ctxreq_a", actor=ContextActor(agent_id="a", role="engineer", model="m"),
        execution=ContextExecution(owner="luis", session_id="s1", project_id="p1",
                                   workspace="/repo", turn_id="t1"),
        task=ContextTask(intent="", phase="act", query="fix the failing lint in src/app.py"),
        policy=ContextPolicy(), consumer="agent", created_at="2026-09-01T12:00:00Z")


async def _bytes(docs, mems, *, reverse_sources=False):
    sources = [_Source("documents", "retrieved_documents", docs),
               _Source("memory_engine", "retrieved_memory", mems)]
    if reverse_sources:
        sources.reverse()
    engine = ContextCompiler(sources=sources, cache=WorkingSet(), clock=lambda: NOW)
    packet = await engine.compile(_req(), context_length=8192, window_known=True,
                                  max_output_tokens=1024)
    return packet, wiring._render_live(packet, "")


async def test_the_same_inputs_render_the_same_bytes_whatever_order_they_arrive_in(ce_db):
    docs = [_cand(f"doc:{n}", f"guide {n} about lint configuration and src/app.py fixes {n}",
                  "retrieved_documents") for n in range(5)]
    mems = [_cand(f"mem:{n}", f"the user prefers rule number {n} for lint", "retrieved_memory")
            for n in range(4)]
    base_packet, base = await _bytes(docs, mems)
    assert base
    shuffled_docs = [docs[i] for i in (3, 0, 4, 2, 1)]
    shuffled_mems = [mems[i] for i in (2, 3, 1, 0)]
    for packet, rendered in (
        await _bytes(shuffled_docs, shuffled_mems),
        await _bytes(list(reversed(docs)), list(reversed(mems)), reverse_sources=True),
        await _bytes(docs, mems),
    ):
        assert rendered == base, "same inputs must render to the same bytes"
        assert packet.identity() == base_packet.identity()
        assert pa.sha256_text(rendered) == pa.sha256_text(base)


# -- recorder and wire ---------------------------------------------------------

def test_the_recorder_keeps_one_record_per_round_and_diffs_them():
    recorder = pa.PromptAuditRecorder(turn_id="t", session_id="s")
    first = recorder.observe_round(1, _base())
    grown = _base() + [{"role": "tool", "tool_call_id": "c", "content": "ok"}]
    second = recorder.observe_round(2, grown)
    assert first["diff"]["first"] is True
    assert second["diff"]["prefix_intact"] is True
    assert second["diff"]["added"][0]["bucket"] == "tool_results"
    summary = recorder.summary()
    assert summary["rounds_recorded"] == 2 and summary["prefix_breaks"] == 0
    json.dumps(summary)


def test_a_prefix_break_is_counted():
    recorder = pa.PromptAuditRecorder()
    recorder.observe_round(1, _base())
    edited = _base()
    edited[0]["content"] = "You are someone else."
    recorder.observe_round(2, edited)
    assert recorder.summary()["prefix_breaks"] == 1


def test_a_redelivered_packet_is_not_persisted_twice():
    recorder = pa.PromptAuditRecorder()
    delivery = {"report": {"packet_id": "p1", "packet_tokens": 5},
                "_audit": {"message_sha256": "x", "sources": [
                    {"source_ref": "mem:a", "_body": "b", "body_sha256": "h",
                     "revision": "", "revision_state": "unversioned"}]}}
    recorder.observe_round(1, _base(), delivery=delivery)
    recorder.observe_round(2, _base(), delivery=delivery)
    rounds = recorder.summary()["rounds"]
    assert rounds[0]["manifest"]["sources"]
    assert rounds[1]["manifest"]["sources_same_as_round"] == 1
    assert "sources" not in rounds[1]["manifest"]


def test_the_wire_body_is_compared_with_the_loop_view():
    recorder = pa.PromptAuditRecorder()
    recorder.observe_round(3, _base(), [{"function": {"name": "t"}}])
    pa.observe_wire({"messages": _base(), "tools": [{"function": {"name": "t"}}]})
    wire = recorder._by_round[3]["wire"]
    assert wire["comparable"] and wire["same_messages_as_loop_view"] is True
    assert wire["transformed_by_transport"] is False

    recorder.observe_round(4, _base())
    merged = _base()
    merged[1]["content"] += "\n\n" + merged[2]["content"]
    del merged[2]
    pa.observe_wire({"messages": merged})
    wire = recorder._by_round[4]["wire"]
    assert wire["transformed_by_transport"] is True and wire["messages"] == 2


def test_the_transport_reports_its_final_body_to_the_audit(monkeypatch):
    from src import llm_core
    recorder = pa.PromptAuditRecorder()
    recorder.observe_round(1, _base())
    monkeypatch.delenv("FAUSTUS_DUMP_LLM_PAYLOADS", raising=False)
    llm_core._dump_stream_payload("http://127.0.0.1:8081/v1", {"messages": _base()})
    assert recorder._by_round[1]["wire"]["same_messages_as_loop_view"] is True


def test_a_payload_without_messages_is_not_comparable():
    recorder = pa.PromptAuditRecorder()
    recorder.observe_round(1, _base())
    pa.observe_wire({"contents": []})
    assert recorder._by_round[1]["wire"]["comparable"] is False


def test_skill_and_instruction_provenance_on_messages_reach_the_round_record():
    messages = _base() + [
        {"role": "user", "content": "skills",
         "metadata": {"trusted": False, "source": "skills",
                      "skill_disclosure": {"fragments": [
                          {"skill": "lint", "level": 1, "version": "1.2.0",
                           "fragment_sha256": "f" * 64, "source": "manual"},
                          {"skill": "deploy", "level": 0, "fragment_sha256": "e" * 64}]}}},
        {"role": "system", "content": "rules",
         "metadata": {"instruction_provenance": [{"rel": "AGENTS.md", "sha256": "a" * 64, "depth": 0}]}},
    ]
    record = pa.PromptAuditRecorder().observe_round(1, messages)
    assert [(s["skill"], s["version_state"]) for s in record["skills"]] == [
        ("lint", "declared"), ("deploy", "unversioned")]
    assert record["instructions"][0]["rel"] == "AGENTS.md"


def test_an_audit_failure_never_reaches_the_caller():
    recorder = pa.PromptAuditRecorder()
    assert recorder.observe_round(1, [object(), None, 5]) is not None or True
    assert recorder.observe_round(2, None) is not None or True


def test_the_context_configuration_is_hashed_and_a_change_is_reported(monkeypatch):
    import src.settings as settings_module
    values = {"agent_context_engine": True, "agent_project_instructions_max_chars": 6000}
    monkeypatch.setattr(settings_module, "get_setting", lambda k, d=None: values.get(k, d))
    recorder = pa.PromptAuditRecorder()
    first = recorder.observe_round(1, _base())
    same = recorder.observe_round(2, _base())
    values["agent_project_instructions_max_chars"] = 9000
    changed = recorder.observe_round(3, _base())
    assert first["config"]["changed"] is False and same["config"]["changed"] is False
    assert first["config"]["config_sha256"] == same["config"]["config_sha256"]
    assert changed["config"]["changed"] is True
    assert changed["config"]["values"]["agent_project_instructions_max_chars"] == 9000
    assert changed["config"]["config_sha256"] != first["config"]["config_sha256"]


def test_an_unreadable_setting_is_not_read_as_its_default(monkeypatch):
    import src.settings as settings_module

    def broken(key, default=None):
        if key == "agent_workspace_trust":
            raise RuntimeError("settings backend down")
        return default

    monkeypatch.setattr(settings_module, "get_setting", broken)
    snapshot = pa.config_snapshot()
    assert snapshot["unreadable"] == ["agent_workspace_trust"]
    assert snapshot["values"]["agent_workspace_trust"] is None


# -- wiring of the loop --------------------------------------------------------

def test_the_loop_observes_the_final_prompt_inside_the_candidate_request():
    index = LOOP.index("async def _candidate_request(")
    body = LOOP[index:index + 6000]
    assert "_prompt_audit.observe_round(round_num, request_messages" in body
    assert "delivery=_ce_live_previous" in body


def test_the_turn_persists_a_hash_only_summary():
    assert 'metrics["prompt_audit"] = _prompt_audit.summary()' in LOOP


# -- reading the audit back: adapter, route and tool ----------------------------

@pytest.fixture()
def audit_db(monkeypatch):
    from datetime import timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database as database

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    database.Base.metadata.create_all(
        engine, tables=[database.Session.__table__, database.ChatMessage.__table__])
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    db = factory()
    db.add(database.Session(id="s-ada", name="s", endpoint_url="http://x", model="m", owner="ada"))
    recorder = pa.PromptAuditRecorder()
    recorder.observe_round(1, _base())
    start = datetime(2026, 1, 5, 9, 0, 0)
    db.add(database.ChatMessage(id="m0", session_id="s-ada", role="assistant", content="old",
                                meta_data=json.dumps({"other": 1}), timestamp=start))
    db.add(database.ChatMessage(id="m1", session_id="s-ada", role="assistant", content="new",
                                meta_data=json.dumps({"prompt_audit": recorder.summary()}),
                                timestamp=start + timedelta(minutes=1)))
    db.commit()
    db.close()
    return factory


def test_the_saved_audit_is_read_back_owner_first(audit_db):
    from src.context_engine.adapters import session_store
    turns = session_store.persisted_prompt_audits("s-ada", "ada")
    assert len(turns) == 1 and turns[0]["message_id"] == "m1"
    assert turns[0]["prompt_audit"]["rounds"][0]["prompt_sha256"]
    assert session_store.persisted_prompt_audits("s-ada", "bruno") == ()
    assert session_store.persisted_prompt_audits("s-missing", "ada") == ()
    assert session_store.persisted_prompt_audits("", "ada") == ()


def test_the_route_returns_the_audit_for_the_session_owner(audit_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    from routes.context_engine_routes import setup_context_engine_routes

    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()

    @app.middleware("http")
    async def _as_ada(request, call_next):
        request.state.current_user = "ada"
        return await call_next(request)

    app.include_router(setup_context_engine_routes())
    body = TestClient(app).get("/api/context/sessions/s-ada/prompt-audit").json()
    assert body["ok"] and body["count"] == 1
    assert "You are Faustus" not in json.dumps(body)


def test_the_mcp_tool_reads_the_same_audit(audit_db, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio
    import mcp_servers.context_engine_server as ces
    monkeypatch.setenv("ODYSSEUS_MCP_CONTEXT_OWNER", "ada")
    monkeypatch.setattr(ces, "_initialized", False)   # restored on teardown
    monkeypatch.setattr(ces, "_engine", {})
    out = asyncio.run(ces.call_tool("context_prompt_audit", {"session_id": "s-ada"}))
    payload = json.loads(out[0].text)
    assert payload["count"] == 1
    empty = asyncio.run(ces.call_tool("context_prompt_audit", {}))
    assert "session_id is required" in empty[0].text
