"""OBJ-29: what the live packet omits for budget stays one call away.

The promises, each a test below:

* the short id is a pure function of (owner, source_ref) — the same omitted
  source is named the same way on every round, which is what keeps the footer
  (and so the prompt prefix) byte-stable;
* the footer lists at most `FOOTER_ITEMS` entries in a fixed order, and the
  same omitted set renders to the same bytes whatever order it arrived in;
* `context_recall` returns the full text with provenance, only to the owner
  whose packet omitted it;
* a row stored without a body is reopened live through the adapter registry;
* `deliver_round` wires all of it: the compile's budget omissions are stored,
  listed in the packet and reported as `recallable`.
"""

from __future__ import annotations

import asyncio

import pytest

from src.agent_tools.context_recall_tools import ContextRecallTool, _ids
from src.context_engine import compiler as compiler_module
from src.context_engine import recall, store, transforms, wiring
from src.context_engine.budgets import estimator_for
from src.context_engine.contracts import (
    ContextBudget, ContextCandidate, ContextItem, ContextOmission, ContextPacket,
    ContextSection,
)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def _candidate(ref, body, *, title="", section="retrieved_documents",
               source_type="document"):
    return ContextCandidate(candidate_id=f"c-{ref}", source_type=source_type,
                            source_ref=ref, title=title, body=body, section=section)


def _packet(omissions, *, sections=(), owner="ada", packet_id="ctxpkt_r1"):
    return ContextPacket(
        packet_id=packet_id, request_id="ctxreq_r1", owner=owner, session_id="s-1",
        model="m", window=ContextBudget(max_tokens=4096, input_budget=900),
        sections=tuple(sections), omissions=tuple(omissions))


def _budget(ref, source_type="document"):
    return ContextOmission(source_type=source_type, source_ref=ref, reason="budget",
                           detail="400 tokens do not fit in 0")


# ── ids ────────────────────────────────────────────────────────────────────

def test_short_ids_are_deterministic_and_owner_scoped():
    first = recall.short_id("ada", "doc:handbook#3")
    assert first == recall.short_id("ada", "doc:handbook#3")
    assert first != recall.short_id("bruno", "doc:handbook#3")
    assert len(first) == recall.SHORT_ID_CHARS
    for spelling in (first, f"ctx:{first}", f"[ctx:{first}]", f" CTX:{first.upper()} "):
        assert recall.normalize_id(spelling) == first
    assert recall.normalize_id("ctx:nothex!!") == ""
    assert recall.normalize_id("") == ""


def test_only_budget_omissions_are_recallable():
    packet = _packet([
        _budget("doc:a"),
        ContextOmission(source_type="memory", source_ref="mem:secret", reason="unauthorised"),
        ContextOmission(source_type="memory", source_ref="mem:old", reason="stale"),
        _budget("doc:a"),
    ])
    assert [o.source_ref for o in recall.recallable_omissions(packet)] == ["doc:a"]


# ── fit() feeds the collector ──────────────────────────────────────────────

def test_fit_collects_what_it_omits_for_budget_only_inside_the_block():
    big = _candidate("doc:big", "word " * 800, title="Big handbook")
    small = _candidate("doc:small", "short note")
    estimator = estimator_for("m")
    with transforms.collect_budget_omissions() as omitted:
        item, omission = transforms.fit(big, budget_tokens=0, estimator=estimator)
        kept, none = transforms.fit(small, budget_tokens=500, estimator=estimator)
    assert item is None and omission is not None and omission.reason == "budget"
    assert kept is not None and none is None
    assert list(omitted) == ["doc:big"]
    assert omitted["doc:big"].body.startswith("word word")
    # outside the block nothing is collected (and nothing leaks between blocks)
    transforms.fit(big, budget_tokens=0, estimator=estimator)
    with transforms.collect_budget_omissions() as fresh:
        pass
    assert fresh == {}


# ── store + recall ─────────────────────────────────────────────────────────

def test_remember_then_recall_returns_the_full_text_with_provenance():
    body = "The Villanueva deployment uses blue/green switches. " * 40
    packet = _packet([_budget("doc:runbook#2")])
    entries = recall.remember(packet, {"doc:runbook#2": _candidate(
        "doc:runbook#2", body, title="Runbook — deploys")}, owner="ada",
        session_id="s-1", project_id="p-1")
    assert [e["source_ref"] for e in entries] == ["doc:runbook#2"]
    ident = entries[0]["id"]
    assert ident == recall.short_id("ada", "doc:runbook#2")

    results = asyncio.run(recall.recall([f"ctx:{ident}"], owner="ada"))
    assert results[0]["found"] is True
    assert results[0]["content"] == body
    assert results[0]["title"] == "Runbook — deploys"
    assert results[0]["source_ref"] == "doc:runbook#2"
    assert results[0]["packet_id"] == "ctxpkt_r1"
    assert results[0]["project_id"] == "p-1"
    assert results[0]["omitted_because"] == "budget"
    text = recall.render_recall(results)
    assert "doc:runbook#2" in text and "Villanueva" in text


def test_another_owner_cannot_recall_the_same_id():
    packet = _packet([_budget("mem:ada-pref")])
    entries = recall.remember(packet, {"mem:ada-pref": _candidate(
        "mem:ada-pref", "Ada prefers pathlib", source_type="memory",
        section="retrieved_memory")}, owner="ada")
    ident = entries[0]["id"]
    stolen = asyncio.run(recall.recall([ident], owner="bruno"))
    assert stolen[0]["found"] is False and stolen[0]["content"] == ""
    unknown = asyncio.run(recall.recall(["0123456789"], owner="bruno"))
    assert stolen[0]["note"] == unknown[0]["note"], "a probe learns nothing"


def test_a_row_without_a_body_is_reopened_live(monkeypatch):
    from src.context_engine import candidates

    packet = _packet([_budget("file:src/app.py")])
    entries = recall.remember(packet, {}, owner="ada", session_id="s-1")
    ident = entries[0]["id"]

    async def fake_fetch(ref, retrieval, **kw):
        assert retrieval.request.execution.owner == "ada"
        return _candidate(ref, "def main(): ...", source_type="file", section="files")

    monkeypatch.setattr(candidates, "fetch_ref", fake_fetch)
    results = asyncio.run(recall.recall([ident], owner="ada"))
    assert results[0]["found"] is True
    assert results[0]["content"] == "def main(): ..."
    assert results[0]["provenance"].startswith("reopened live")


def test_an_update_keeps_a_body_it_already_had():
    ref = "doc:guide"
    recall.remember(_packet([_budget(ref)]), {ref: _candidate(ref, "full guide text")},
                    owner="ada")
    recall.remember(_packet([_budget(ref)], packet_id="ctxpkt_r2"), {}, owner="ada")
    results = asyncio.run(recall.recall([recall.short_id("ada", ref)], owner="ada"))
    assert results[0]["content"] == "full guide text"
    assert results[0]["packet_id"] == "ctxpkt_r2"


def test_recall_caps_ids_and_skips_duplicates():
    ids = [f"{i:010x}" for i in range(recall.MAX_RECALL_IDS + 5)]
    results = asyncio.run(recall.recall(ids + ids, owner="ada"))
    assert len(results) == recall.MAX_RECALL_IDS


# ── privacy: incognito never feeds the cache, stale sources never answer ────
#
# The recall cache exists so a *budget* cut is reversible; it must never be
# the thing that makes an *incognito* cut, or a forgotten memory, reversible
# too. Two leaks, one cache:
#
#   (a) an incognito/no_memory turn must store nothing at all, and a
#       recent-messages/session item must never be stored even outside
#       incognito — that text is already in the conversation, not a place a
#       separate, longer-lived copy belongs;
#   (b) a stored body for a mem:/pmem:/ent:/note: source must not outlive the
#       source: recall re-checks it and, if it is gone, answers "not found"
#       and deletes the row instead of handing back the last thing it saw.

def test_incognito_turn_stores_nothing():
    ref = "mem:private-1"
    packet = _packet([_budget(ref, source_type="memory")])
    candidate = _candidate(ref, "Bruno's door code is 4711", source_type="memory",
                           section="retrieved_memory")
    entries = recall.remember(packet, {ref: candidate}, owner="bruno",
                              session_id="s-incognito", allow_personal_memory=False)
    assert entries == []
    assert recall._row("bruno", recall.short_id("bruno", ref)) is None


def test_recent_messages_are_never_stored_even_outside_incognito():
    ref = "session:s-1#3"
    packet = _packet([_budget(ref, source_type="message")])
    candidate = _candidate(ref, "the user's last message", source_type="message",
                           section="recent_messages")
    entries = recall.remember(packet, {ref: candidate}, owner="ada",
                              allow_personal_memory=True)
    assert entries == []
    assert recall._row("ada", recall.short_id("ada", ref)) is None


def test_forgotten_memory_is_no_longer_recallable(monkeypatch):
    """r25.py: a memory the owner forgot must not still answer `context_recall`
    just because a packet cached its body before it was forgotten."""
    import src.memory_engine as engine

    ref = "mem:door-code"
    packet = _packet([_budget(ref, source_type="memory")])
    candidate = _candidate(ref, "Bruno's door code is 4711", source_type="memory",
                           section="retrieved_memory")
    entries = recall.remember(packet, {ref: candidate}, owner="bruno")
    ident = entries[0]["id"]

    # sanity: while the memory is still there, the cached body still answers
    monkeypatch.setattr(engine, "get_item",
                        lambda item_id: {"id": item_id, "owner": "bruno",
                                         "suppressed": False, "sensitivity": "normal"})
    assert asyncio.run(recall.recall([ident], owner="bruno"))[0]["found"] is True

    monkeypatch.setattr(engine, "get_item", lambda item_id: None)  # forgotten
    results = asyncio.run(recall.recall([ident], owner="bruno"))
    assert results[0]["found"] is False
    assert "no longer available" in results[0]["note"]
    assert recall._row("bruno", ident) is None, "the stale row must be purged"


def test_suppressed_memory_is_no_longer_recallable(monkeypatch):
    import src.memory_engine as engine

    ref = "mem:suppressed-1"
    entries = recall.remember(
        _packet([_budget(ref, source_type="memory")]),
        {ref: _candidate(ref, "a rule the curator suppressed", source_type="memory")},
        owner="ada")
    ident = entries[0]["id"]
    monkeypatch.setattr(engine, "get_item",
                        lambda item_id: {"id": item_id, "owner": "ada",
                                         "suppressed": True, "sensitivity": "normal"})
    results = asyncio.run(recall.recall([ident], owner="ada"))
    assert results[0]["found"] is False


def test_secret_memory_is_no_longer_recallable(monkeypatch):
    import src.memory_engine as engine

    ref = "mem:secret-1"
    entries = recall.remember(
        _packet([_budget(ref, source_type="memory")]),
        {ref: _candidate(ref, "something marked secret afterwards", source_type="memory")},
        owner="ada")
    ident = entries[0]["id"]
    monkeypatch.setattr(engine, "get_item",
                        lambda item_id: {"id": item_id, "owner": "ada",
                                         "suppressed": False, "sensitivity": "secret"})
    results = asyncio.run(recall.recall([ident], owner="ada"))
    assert results[0]["found"] is False


def test_removed_personal_memory_is_no_longer_recallable(monkeypatch):
    import src.memory as memmod

    ref = "pmem:e1"
    entries = recall.remember(
        _packet([_budget(ref, source_type="memory")]),
        {ref: _candidate(ref, "a personal fact, later removed", source_type="memory")},
        owner="bruno")
    ident = entries[0]["id"]

    class _GoneManager:
        def load(self, owner):
            return []

    monkeypatch.setattr(memmod, "MemoryManager", lambda data_dir: _GoneManager())
    results = asyncio.run(recall.recall([ident], owner="bruno"))
    assert results[0]["found"] is False


def test_hidden_entity_is_no_longer_recallable(monkeypatch):
    from src.brain import entities

    ref = "ent:e-1"
    entries = recall.remember(
        _packet([_budget(ref, source_type="memory")]),
        {ref: _candidate(ref, "Cordera Labs (organization)", source_type="memory")},
        owner="ada")
    ident = entries[0]["id"]
    monkeypatch.setattr(entities, "get_entity",
                        lambda entity_id: {"id": entity_id, "owner": "ada", "hidden": True})
    results = asyncio.run(recall.recall([ident], owner="ada"))
    assert results[0]["found"] is False


def test_deleted_note_is_no_longer_recallable(monkeypatch):
    from src.brain import notes

    ref = "note:Notes/plan.md"
    entries = recall.remember(
        _packet([_budget(ref, source_type="memory")]),
        {ref: _candidate(ref, "a note later deleted", source_type="memory")},
        owner="ada")
    ident = entries[0]["id"]

    def _missing(owner, path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(notes, "read_note", _missing)
    results = asyncio.run(recall.recall([ident], owner="ada"))
    assert results[0]["found"] is False


def test_still_present_mem_source_is_returned_unaffected(monkeypatch):
    """The recheck must not punish sources that are still there."""
    import src.memory_engine as engine

    ref = "mem:still-here"
    entries = recall.remember(
        _packet([_budget(ref, source_type="memory")]),
        {ref: _candidate(ref, "Ada prefers pathlib", source_type="memory")},
        owner="ada")
    ident = entries[0]["id"]
    monkeypatch.setattr(engine, "get_item",
                        lambda item_id: {"id": item_id, "owner": "ada",
                                         "suppressed": False, "sensitivity": "normal"})
    results = asyncio.run(recall.recall([ident], owner="ada"))
    assert results[0]["found"] is True
    assert results[0]["content"] == "Ada prefers pathlib"


def test_purge_source_deletes_every_cached_row_for_that_source():
    ref = "mem:to-purge"
    entries = recall.remember(
        _packet([_budget(ref, source_type="memory")]),
        {ref: _candidate(ref, "will be purged on forget", source_type="memory")},
        owner="ada")
    ident = entries[0]["id"]
    assert recall._row("ada", ident) is not None
    assert recall.purge_source("ada", ref) == 1
    assert recall._row("ada", ident) is None
    assert recall.purge_source("ada", ref) == 0


# ── the footer ─────────────────────────────────────────────────────────────

def test_the_footer_is_byte_stable_for_the_same_omitted_set():
    entries = [
        {"id": recall.short_id("ada", f"doc:d{i}"), "source_ref": f"doc:d{i}",
         "section": "retrieved_documents", "title": f"Doc {i}"}
        for i in range(5)
    ]
    first = recall.render_footer(entries)
    second = recall.render_footer(list(reversed(entries)))
    assert first == second
    assert first.count("[ctx:") == 5
    assert "context_recall" in first


def test_the_footer_lists_at_most_footer_items_and_says_how_many_more():
    entries = [{"id": recall.short_id("ada", f"doc:{i:02d}"), "source_ref": f"doc:{i:02d}",
                "section": "retrieved_documents", "title": ""}
               for i in range(recall.FOOTER_ITEMS + 3)]
    footer = recall.render_footer(entries)
    assert footer.count("[ctx:") == recall.FOOTER_ITEMS
    assert "(+3 more omitted, not listed)" in footer
    assert recall.render_footer([]) == ""


def test_footer_lines_are_single_line_and_carry_the_source():
    line = recall.footer_lines([{"id": "ab12cd34ef", "source_ref": "mem:x",
                                 "title": "Multi\nline   title", "section": "s"}])[0]
    assert line == "[ctx:ab12cd34ef] Multi line title (mem:x)"


# ── the tool ───────────────────────────────────────────────────────────────

def test_the_tool_accepts_the_shapes_models_write():
    assert _ids('{"ids": ["ctx:ab12cd34ef", "0123456789"]}') == ["ctx:ab12cd34ef",
                                                                  "0123456789"]
    assert _ids('{"id": "ctx:ab12cd34ef"}') == ["ab12cd34ef"]
    assert _ids("[ctx:ab12cd34ef] Runbook (doc:x)") == ["ab12cd34ef"]
    assert _ids("") == []


def test_the_tool_uses_the_runtime_owner():
    ref = "doc:plan"
    ident = recall.remember(_packet([_budget(ref)]), {ref: _candidate(ref, "the plan")},
                            owner="ada")[0]["id"]
    tool = ContextRecallTool()
    mine = asyncio.run(tool.execute(f'{{"ids": ["ctx:{ident}"], "owner": "bruno"}}',
                                    {"owner": "ada"}))
    assert mine["exit_code"] == 0 and mine["found"] == 1
    assert "the plan" in mine["output"]
    theirs = asyncio.run(tool.execute(f'{{"ids": ["{ident}"]}}', {"owner": "bruno"}))
    assert theirs["exit_code"] == 1 and theirs["found"] == 0
    missing = asyncio.run(tool.execute("{}", {"owner": "ada"}))
    assert missing["exit_code"] == 1 and "ids" in missing["error"]


def test_registered_in_every_place_the_repo_requires():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_capabilities import ResultIntegrity, ToolEffect, capabilities_for_tool
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

    name = "context_recall"
    assert name in TOOL_HANDLERS and name in TOOL_TAGS
    schema = next(t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == name)
    assert schema["function"]["parameters"]["required"] == ["ids"]
    assert BUILTIN_TOOL_DESCRIPTIONS.get(name)
    assert len(EXAMPLES.get(name, [])) >= 2
    caps = capabilities_for_tool(name)
    assert caps.known and ToolEffect.READ_PRIVATE in caps.effects
    assert caps.result_integrity is ResultIntegrity.EXTERNAL_UNTRUSTED
    block = function_call_to_tool_block(name, '{"ids": ["ctx:ab12cd34ef"]}')
    assert block is not None and block.tool_type == name
    assert '"ab12cd34ef"' in block.content or "ctx:ab12cd34ef" in block.content


def test_the_mcp_server_exposes_it():
    pytest.importorskip("mcp")
    import mcp_servers.context_engine_server as ces

    tools = {t.name for t in asyncio.run(ces.list_tools())}
    assert "context_recall" in tools
    assert "context_recall" in ces._ASYNC_HANDLERS


# ── deliver_round wires it ─────────────────────────────────────────────────

@pytest.fixture()
def live(monkeypatch):
    import src.settings as settings_module

    values = {"agent_context_engine": True, "agent_context_timeout_ms": 2000}
    monkeypatch.setattr(settings_module, "get_setting",
                        lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(wiring, "_live_budget", lambda *a, **k: 900)
    monkeypatch.setattr("src.context_selection.policy_overrides",
                        lambda *a, **k: {"excluded_refs": (), "excluded_prefixes": (),
                                         "pinned_refs": ()})

    class OmittingCompiler:
        async def compile(self, request, **kw):
            estimator = estimator_for("m")
            omissions = []
            for ref, body in (("doc:zeta", "Zeta notes " * 300),
                              ("doc:alpha", "Alpha notes " * 300)):
                _, omission = transforms.fit(_candidate(ref, body, title=ref.upper()),
                                             budget_tokens=0, estimator=estimator)
                omissions.append(omission)
            return ContextPacket(
                packet_id="ctxpkt_live", request_id=request.request_id,
                owner=request.execution.owner, session_id="s-1", model="m",
                window=ContextBudget(max_tokens=4096, input_budget=900),
                sections=(ContextSection(kind="retrieved_memory", items=(
                    ContextItem(item_id="i1", source_type="memory", source_ref="mem:one",
                                title="Known preference", body="Use concise prose",
                                tokens=6),)),),
                omissions=tuple(omissions))

    monkeypatch.setattr(compiler_module, "compiler", lambda: OmittingCompiler())


def _deliver():
    request = wiring.build_request(owner="ada", session_id="s-1", model="m",
                                   project_id="p-1",
                                   messages=[{"role": "user", "content": "deploy?"}])
    return asyncio.run(wiring.deliver_round(
        request=request, messages=[{"role": "user", "content": "deploy?"}],
        context_length=4096, window_known=True))


def test_deliver_round_lists_and_stores_the_budget_omissions(live):
    result = _deliver()
    assert result is not None
    content = result["message"]["content"]
    ids = result["report"]["recallable"]
    assert ids == [recall.short_id("ada", "doc:zeta"), recall.short_id("ada", "doc:alpha")]
    assert "## omitted_for_budget" in content
    alpha, zeta = (content.index(f"[ctx:{recall.short_id('ada', r)}]")
                   for r in ("doc:alpha", "doc:zeta"))
    assert alpha < zeta, "rendered in (section, source_ref) order"
    assert content.index("Use concise prose") < alpha, "the footer comes last"
    back = asyncio.run(ContextRecallTool().execute({"ids": ids[:1]}, {"owner": "ada"}))
    assert back["found"] == 1 and "Zeta notes" in back["output"]


def test_the_packet_text_is_byte_stable_across_rounds(live):
    first = _deliver()["message"]["content"]
    second = _deliver()["message"]["content"]
    assert first == second


def test_a_broken_recall_store_costs_only_the_footer(live, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(recall, "remember", boom)
    result = _deliver()
    assert result is not None
    assert "omitted_for_budget" not in result["message"]["content"]
    assert result["report"]["recallable"] == []
    assert "Use concise prose" in result["message"]["content"]


def test_no_live_packet_when_the_room_is_below_the_compiler_floor(live, monkeypatch):
    """`resolve_budget` never goes below MIN_INPUT_BUDGET; delivering with less
    room than that would let the packet overrun what is really left."""
    from src.context_engine.budgets import MIN_INPUT_BUDGET

    monkeypatch.setattr(wiring, "_live_budget", lambda *a, **k: MIN_INPUT_BUDGET - 1)
    assert _deliver() is None
    monkeypatch.setattr(wiring, "_live_budget", lambda *a, **k: MIN_INPUT_BUDGET)
    assert _deliver() is not None
