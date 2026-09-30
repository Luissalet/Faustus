"""H12: canonical history, one projection per protocol, a receipt per repair.

Acceptance: repairing the prompt neither deletes nor invents effects.
"""
import copy
import json
import random

import pytest

from src import history_projection as hp
from src import llm_core
from src import settings as _settings
from src.context_compactor import _sanitize_tool_messages

IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


@pytest.fixture(autouse=True)
def _clean_log():
    hp.reset_receipts_for_tests()
    yield
    hp.reset_receipts_for_tests()


@pytest.fixture
def setting(monkeypatch):
    real = _settings.get_setting
    values = {}

    def _set(key, value):
        values[key] = value
        monkeypatch.setattr(_settings, "get_setting",
                            lambda k, d=None: values[k] if k in values else real(k, d))
    return _set


def call(call_id, name="read_file", args='{"path":"a.txt"}'):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def assistant(calls, content=None, **extra):
    return {"role": "assistant", "content": content, "tool_calls": calls, **extra}


def tool(call_id, content="ok", **extra):
    return {"role": "tool", "tool_call_id": call_id, "content": content, **extra}


def kinds(receipts):
    return [r.kind for r in receipts]


# ── canonical form ───────────────────────────────────────────────────────────

def test_answered_calls_keep_real_ids_and_need_no_repair():
    msgs = [{"role": "user", "content": "go"},
            assistant([call("c1"), call("c2", "grep")], "looking"),
            tool("c2", "second"), tool("c1", "first"),
            {"role": "assistant", "content": "done"}]
    proj = hp.project(msgs)
    assert proj.receipts == []
    # results stay in the order they were recorded
    assert [m.get("tool_call_id") for m in proj.messages if m["role"] == "tool"] == ["c2", "c1"]
    history = proj.canonical.to_dict()
    exchange = history["entries"][1]
    assert [c["id"] for c in exchange["calls"]] == ["c1", "c2"]
    assert all(c["certainty"] == "recorded" and not c["result_synthetic"] for c in exchange["calls"])


def test_effect_status_on_a_result_is_kept_as_certainty():
    msgs = [assistant([call("c1", "send_email")]), tool("c1", "timeout after send", _effect_status="outcome_unknown")]
    proj = hp.project(msgs)
    [entry] = proj.canonical.entries
    assert entry.calls[0].result.certainty == "outcome_unknown"
    # the private key never reaches the provider
    assert "_effect_status" not in proj.messages[1]


def test_unanswered_call_is_kept_with_an_explicit_unknown_result_and_a_receipt():
    msgs = [{"role": "user", "content": "send it"},
            assistant([call("c9", "send_email", '{"to":"x"}')], "sending")]
    original = copy.deepcopy(msgs)
    proj = hp.project(msgs)
    assert msgs == original
    assert [m["role"] for m in proj.messages] == ["user", "assistant", "tool"]
    assert proj.messages[1]["tool_calls"][0]["id"] == "c9"
    assert proj.messages[2] == {"role": "tool", "tool_call_id": "c9", "content": hp.UNKNOWN_RESULT_TEXT}
    [receipt] = proj.receipts
    assert receipt.kind == hp.R_UNKNOWN_RESULT_ADDED
    assert receipt.call_id == "c9" and receipt.tool == "send_email"
    assert "send_email" in json.dumps(proj.receipt_dicts()) or receipt.detail["arguments_preview"]
    assert "to" in receipt.detail["arguments_preview"]
    assert proj.canonical.to_dict()["entries"][1]["calls"][0]["certainty"] == "unknown"


def test_partially_answered_call_keeps_the_unanswered_one():
    msgs = [assistant([call("a"), call("b"), call("c")]), tool("b", "B")]
    proj = hp.project(msgs)
    assert [c["id"] for c in proj.messages[0]["tool_calls"]] == ["a", "b", "c"]
    results = {m["tool_call_id"]: m["content"] for m in proj.messages if m["role"] == "tool"}
    assert results == {"a": hp.UNKNOWN_RESULT_TEXT, "b": "B", "c": hp.UNKNOWN_RESULT_TEXT}
    assert sorted(r.call_id for r in proj.receipts) == ["a", "c"]


def test_call_without_an_id_gets_a_synthetic_id_not_deleted():
    bad = {"type": "function", "function": {"name": "bash", "arguments": '{"cmd":"rm x"}'}}
    proj = hp.project([assistant([bad], "x")])
    sent = proj.messages[0]["tool_calls"][0]
    assert sent["id"].startswith("faustus_noid_") and sent["function"]["name"] == "bash"
    assert proj.messages[1]["tool_call_id"] == sent["id"]
    assert hp.R_CALL_ID_ASSIGNED in kinds(proj.receipts)
    assert hp.R_UNKNOWN_RESULT_ADDED in kinds(proj.receipts)
    assert "rm x" in proj.receipts[0].detail["arguments_preview"]


def test_result_after_intervening_messages_is_matched_not_orphaned_and_call_not_unknown():
    msgs = [assistant([call("c1")]),
            {"role": "user", "content": "meanwhile"},
            tool("c1", "real result")]
    proj = hp.project(msgs)
    assert [m["role"] for m in proj.messages] == ["assistant", "tool", "user"]
    assert proj.messages[1]["content"] == "real result"
    assert kinds(proj.receipts) == [hp.R_RESULT_MOVED]
    assert hp.UNKNOWN_RESULT_TEXT not in json.dumps(proj.messages)


def test_orphan_result_is_not_projected_but_receipted_with_a_digest():
    msgs = [{"role": "user", "content": "hi"}, tool("gone", "secret output of a cut call")]
    proj = hp.project(msgs)
    assert [m["role"] for m in proj.messages] == ["user"]
    [receipt] = proj.receipts
    assert receipt.kind == hp.R_ORPHAN_RESULT and receipt.call_id == "gone"
    assert receipt.detail["content_chars"] == len("secret output of a cut call")
    assert "secret output" not in json.dumps(receipt.to_dict())
    # the canonical history still holds it
    assert any(isinstance(e, hp.Orphan) for e in proj.canonical.entries)


def test_duplicate_result_keeps_the_first():
    msgs = [assistant([call("c1")]), tool("c1", "first"), tool("c1", "second")]
    proj = hp.project(msgs)
    assert [m["content"] for m in proj.messages if m["role"] == "tool"] == ["first"]
    assert kinds(proj.receipts) == [hp.R_DUPLICATE_RESULT]


def test_ids_reused_by_a_later_turn_do_not_steal_results():
    msgs = [assistant([call("call_1_0")]), tool("call_1_0", "turn one"),
            {"role": "user", "content": "again"},
            assistant([call("call_1_0")]), tool("call_1_0", "turn two")]
    proj = hp.project(msgs)
    assert proj.receipts == []
    assert [m["content"] for m in proj.messages if m["role"] == "tool"] == ["turn one", "turn two"]


def test_legacy_marker_is_rewritten_with_a_receipt_but_a_marker_with_an_image_is_kept():
    msgs = [{"role": "assistant", "content": "Reference context received."},
            {"role": "assistant", "content": [{"type": "text", "text": "Reference context received."}, IMG]}]
    proj = hp.project(msgs)
    assert proj.messages[0]["content"] == hp.BOUNDARY
    assert proj.messages[1]["content"][1] == IMG
    assert kinds(proj.receipts) == [hp.R_LEGACY_MARKER]


def test_projection_does_not_modify_its_input_and_is_idempotent():
    msgs = [{"role": "system", "content": "s", "junk": 1},
            {"role": "user", "content": [{"type": "text", "text": "t"}, IMG]},
            assistant([call("a"), call("b")], [{"type": "text", "text": "x"}, IMG], reasoning_content="r"),
            tool("a", "A"), {"role": "user", "content": "u"}, {"role": "user", "content": "u2"}]
    original = copy.deepcopy(msgs)
    first = hp.project(msgs)
    assert msgs == original
    second = hp.project(first.messages)
    assert second.messages == first.messages
    assert [r for r in second.receipts if r.kind != hp.R_USERS_MERGED] == []


def test_strict_alternation_inserts_a_boundary_only_for_anthropic_with_blocks():
    untrusted = {"role": "user", "content": [
        {"type": "text", "text": "UNTRUSTED SOURCE DATA\nx\n<<<UNTRUSTED_SOURCE_DATA>>>\nb"}]}
    msgs = [untrusted, {"role": "user", "content": "q"}]
    assert [m["role"] for m in hp.project(msgs, provider="anthropic").messages] == ["user", "assistant", "user"]
    assert [m["role"] for m in hp.project(msgs, provider=None).messages] == ["user", "user"]


# ── the effects invariant, over generated histories ──────────────────────────

def _generate(rng):
    msgs, pending, n = [], [], 0
    for i in range(rng.randint(1, 10)):
        kind = rng.choice(["sys", "user", "blocks", "untrusted", "asst", "marker", "img", "calls", "calls",
                           "tool", "tool", "orphan", "empty", "noid"])
        if kind == "sys":
            msgs.append({"role": "system", "content": f"s{i}"})
        elif kind == "user":
            msgs.append({"role": "user", "content": f"u{i}"})
        elif kind == "blocks":
            msgs.append({"role": "user", "content": [{"type": "text", "text": "t"}, IMG]})
        elif kind == "untrusted":
            msgs.append({"role": "user", "content": "UNTRUSTED SOURCE DATA\nx\n<<<UNTRUSTED_SOURCE_DATA>>>\nb"})
        elif kind == "asst":
            msgs.append({"role": "assistant", "content": f"a{i}", "reasoning_content": rng.choice([None, "r"])})
        elif kind == "marker":
            msgs.append({"role": "assistant", "content": rng.choice(list(hp.BOUNDARY_ALIASES))})
        elif kind == "img":
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "x"}, IMG]})
        elif kind == "empty":
            msgs.append({"role": "user"})
        elif kind == "noid":
            msgs.append(assistant([{"type": "function", "function": {"name": "t", "arguments": "{}"}}]))
        elif kind == "calls":
            calls = []
            for _ in range(rng.randint(1, 3)):
                n += 1
                calls.append(call(f"c{n}", "t", "{}"))
                pending.append(f"c{n}")
            msgs.append(assistant(calls, rng.choice([None, "ok", "", [{"type": "text", "text": "b"}]])))
        elif kind == "tool" and pending:
            cid = pending.pop(rng.randrange(len(pending)))
            msgs.append(tool(cid, "r" + cid, _tool_round=1, metadata={"x": 1}))
        elif kind == "orphan":
            msgs.append(tool(rng.choice(["zz", "c1", "c2"]), "o"))
    return msgs


def _ids_in(messages):
    return [tc.get("id") for m in messages if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)]


@pytest.mark.parametrize("provider", [None, "anthropic"])
def test_generated_histories_keep_every_call_and_invent_no_result(provider):
    rng = random.Random(11)
    for _ in range(1500):
        msgs = _generate(rng)
        original = copy.deepcopy(msgs)
        proj = hp.project(msgs, provider=provider, record=False)
        assert msgs == original
        sent = proj.messages
        sent_calls = _ids_in(sent)
        # no call with a real id disappears, none is duplicated
        real_ids = [tc["id"] for m in msgs if m.get("role") == "assistant"
                    for tc in (m.get("tool_calls") or []) if isinstance(tc, dict) and tc.get("id")]
        assert [i for i in sent_calls if not str(i).startswith("faustus_noid_")] == real_ids
        # every projected call has exactly one result, right after its message
        results = [m for m in sent if m.get("role") == "tool"]
        assert sorted(m["tool_call_id"] for m in results) == sorted(sent_calls)
        # a result is synthetic only when the source had no result for that call
        # after the message that made it (a result cannot precede its call)
        call_pos = {tc["id"]: i for i, m in enumerate(msgs) if m.get("role") == "assistant"
                    for tc in (m.get("tool_calls") or []) if isinstance(tc, dict) and tc.get("id")}
        later = {}
        for i, m in enumerate(msgs):
            if m.get("role") == "tool" and i > call_pos.get(m.get("tool_call_id"), 10 ** 9):
                later.setdefault(m["tool_call_id"], m)
        for m in results:
            if m["content"] == hp.UNKNOWN_RESULT_TEXT:
                assert m["tool_call_id"] not in later
            else:
                assert m["content"] == later[m["tool_call_id"]]["content"]
        # adjacency: a tool message directly follows its assistant or another tool message
        for idx, m in enumerate(sent):
            if m.get("role") == "tool":
                assert idx > 0 and sent[idx - 1]["role"] in ("assistant", "tool")
        again = hp.project(sent, provider=provider, record=False)
        assert again.messages == sent


INTENDED = {hp.R_UNKNOWN_RESULT_ADDED, hp.R_CALL_ID_ASSIGNED, hp.R_RESULT_MOVED, hp.R_MALFORMED_CALL}


@pytest.mark.parametrize("provider", [None, "anthropic"])
def test_canonical_openai_projection_equals_the_previous_repair_except_receipted_differences(provider):
    rng = random.Random(7)
    compared = 0
    for _ in range(2500):
        msgs = _generate(rng)
        proj = hp.project(msgs, provider=provider, record=False)
        if INTENDED & set(kinds(proj.receipts)):
            continue
        assert llm_core._sanitize_llm_messages_legacy(msgs, provider=provider) == proj.messages
        compared += 1
    assert compared > 800


def test_canonical_anthropic_projection_equals_the_previous_conversion():
    rng = random.Random(5)
    compared = 0
    for _ in range(2500):
        msgs = _generate(rng)
        repaired = llm_core._sanitize_llm_messages_legacy(msgs, provider="anthropic")
        if INTENDED & set(kinds(hp.project(msgs, record=False).receipts)):
            continue
        legacy_system, legacy_chat = llm_core._anthropic_messages_legacy(repaired)
        proj = hp.project(repaired, "anthropic", record=False)
        assert proj.system_parts == legacy_system and proj.messages == legacy_chat
        compared += 1
    assert compared > 800


def test_canonical_ollama_projection_equals_the_previous_conversion():
    rng = random.Random(3)
    compared = 0
    for _ in range(2500):
        msgs = _generate(rng)
        if INTENDED & set(kinds(hp.project(msgs, record=False).receipts)):
            continue
        repaired = llm_core._sanitize_llm_messages_legacy(msgs)
        assert hp.project(repaired, "ollama", record=False).messages == llm_core._ollama_normalize_messages(repaired)
        compared += 1
    assert compared > 800


# ── protocols ────────────────────────────────────────────────────────────────

def test_anthropic_projection_carries_ids_and_an_unknown_result_for_a_missing_one():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
            assistant([call("t1", "read_file", '{"path":"a"}'), call("t2", "bash", '{"cmd":"x"}')], "ok"),
            tool("t1", "A")]
    proj = hp.project(msgs, "anthropic")
    assert proj.system_parts == ["sys"]
    roles = [m["role"] for m in proj.messages]
    assert roles == ["user", "assistant", "user", "user"]
    uses = [b for b in proj.messages[1]["content"] if b["type"] == "tool_use"]
    assert [(b["id"], b["name"], b["input"]) for b in uses] == [
        ("t1", "read_file", {"path": "a"}), ("t2", "bash", {"cmd": "x"})]
    results = {m["content"][0]["tool_use_id"]: m["content"][0]["content"] for m in proj.messages[2:]}
    assert results == {"t1": "A", "t2": hp.UNKNOWN_RESULT_TEXT}


def test_build_anthropic_payload_uses_the_canonical_projection():
    payload = llm_core._build_anthropic_payload(
        "model-x", [{"role": "user", "content": "go"}, assistant([call("t1")], "x")], 0.2, 100)
    block = payload["messages"][-1]["content"][0]
    assert (block["type"], block["tool_use_id"], block["content"]) == (
        "tool_result", "t1", hp.UNKNOWN_RESULT_TEXT)


def test_ollama_projection_has_object_arguments_split_images_and_the_unknown_result():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "look"}, IMG]},
            assistant([call("o1", "read_file", '{"path":"a"}')])]
    proj = hp.project(msgs, "ollama")
    assert proj.messages[0] == {"role": "user", "content": "look", "images": ["AAAA"]}
    assert proj.messages[1]["tool_calls"][0]["function"]["arguments"] == {"path": "a"}
    assert proj.messages[2]["content"] == hp.UNKNOWN_RESULT_TEXT
    payload = llm_core._build_ollama_payload("m", msgs, 0.1, 50)
    assert payload["messages"][2]["content"] == hp.UNKNOWN_RESULT_TEXT


def test_text_fence_projection_renders_calls_as_fences_and_results_as_untrusted_text():
    msgs = [{"role": "user", "content": "go"},
            assistant([call("f1", "read_file", '{"path":"a.txt"}'), call("f2", "bash", '{"cmd":"ls"}')], "checking"),
            tool("f1", "file text", _effect_status="partial")]
    proj = hp.project(msgs, "text_fence")
    assert all("tool_calls" not in m and m["role"] != "tool" for m in proj.messages)
    assert [m["role"] for m in proj.messages] == ["user", "assistant", "user"]
    assistant_text = proj.messages[1]["content"]
    assert assistant_text.startswith("checking")
    assert '```read_file\n{"path":"a.txt"}\n```' in assistant_text
    assert '```bash\n{"cmd":"ls"}\n```' in assistant_text
    body = proj.messages[2]["content"]
    assert body.startswith("UNTRUSTED SOURCE DATA")
    assert "[read_file result, call f1, status: partial]\nfile text" in body
    # the call with no recorded result is an explicit item, not silence
    assert "[bash result, call f2, status: unknown]" in body and hp.UNKNOWN_RESULT_TEXT in body
    assert hp.project(proj.messages, "text_fence").messages == proj.messages


def test_text_fence_with_block_content_keeps_the_blocks():
    msgs = [assistant([call("f1")], [{"type": "text", "text": "see"}, IMG]), tool("f1", "r")]
    out = hp.project(msgs, "text_fence").messages
    assert out[0]["content"][1] == IMG and out[0]["content"][-1]["text"].startswith("```read_file")


def test_unknown_protocol_is_refused():
    with pytest.raises(ValueError):
        hp.project([], "made_up")


# ── wiring: modes and the no-tools fence switch ──────────────────────────────

UNANSWERED = [{"role": "user", "content": "go"}, assistant([call("c1", "send_email")], "sending")]


def test_default_mode_keeps_the_unanswered_call():
    out = llm_core._sanitize_llm_messages(UNANSWERED)
    assert [m["role"] for m in out] == ["user", "assistant", "tool"]


def test_legacy_mode_restores_the_previous_pruning(setting):
    setting("llm_projection_mode", "legacy")
    assert llm_core._sanitize_llm_messages(UNANSWERED) == [
        {"role": "user", "content": "go"}, {"role": "assistant", "content": "sending"}]
    assert llm_core._anthropic_messages(llm_core._sanitize_llm_messages(UNANSWERED))[1] == [
        {"role": "user", "content": "go"}, {"role": "assistant", "content": "sending"}]


def test_shadow_mode_sends_legacy_and_records_the_difference(setting):
    setting("llm_projection_mode", "shadow")
    out = llm_core._sanitize_llm_messages(UNANSWERED)
    assert out == [{"role": "user", "content": "go"}, {"role": "assistant", "content": "sending"}]
    rows = hp.recent_receipts()
    assert [r["kind"] for r in rows] == [hp.R_SHADOW_DIFFERENCE]
    assert rows[0]["detail"]["legacy_len"] == 2 and rows[0]["detail"]["canonical_len"] == 3
    hp.reset_receipts_for_tests()
    # no difference, nothing recorded
    assert llm_core._sanitize_llm_messages([{"role": "user", "content": "x"}]) == [{"role": "user", "content": "x"}]
    assert hp.recent_receipts() == []


def test_unknown_mode_value_falls_back_to_canonical(setting):
    setting("llm_projection_mode", "nonsense")
    assert hp.projection_mode() == "canonical"


def test_repairs_are_logged_with_counts():
    llm_core._sanitize_llm_messages(UNANSWERED)
    llm_core._sanitize_llm_messages([tool("x", "o")])
    counts = hp.receipt_counts()
    assert counts[hp.R_UNKNOWN_RESULT_ADDED] == 1 and counts[hp.R_ORPHAN_RESULT] == 1
    rows = hp.recent_receipts(10)
    assert rows[0]["protocol"] == "openai_chat" and rows[0]["reason"]


def test_history_protocol_fences_only_without_tools_and_when_enabled(setting):
    assert llm_core._history_protocol(None) == "openai_chat"
    setting("llm_fence_history_without_tools", True)
    assert llm_core._history_protocol(None) == "text_fence"
    assert llm_core._history_protocol([{"type": "function"}]) == "openai_chat"
    out = llm_core._sanitize_llm_messages(
        [{"role": "user", "content": "go"}, assistant([call("c1")]), tool("c1", "r")],
        protocol=llm_core._history_protocol(None))
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert "tool_calls" not in out[1]


# ── the compactor shares the same repair ─────────────────────────────────────

def test_compactor_keeps_a_call_whose_result_was_trimmed_and_private_keys():
    msgs = [{"role": "user", "content": "go"},
            assistant([call("c1"), call("c2")], "x", _keep="yes"),
            tool("c1", "one", _tool_round=3)]
    out = _sanitize_tool_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool"]
    assert out[1]["_keep"] == "yes"
    assert out[2]["_tool_round"] == 3
    assert out[3]["tool_call_id"] == "c2" and out[3]["content"] == hp.OMITTED_RESULT_TEXT
    assert hp.receipt_counts()[hp.R_UNKNOWN_RESULT_ADDED] == 1
    assert msgs[1]["tool_calls"][1]["id"] == "c2"


def test_compactor_drops_a_result_whose_call_was_cut_with_a_receipt():
    out = _sanitize_tool_messages([tool("cut", "x"), {"role": "user", "content": "hi"}])
    assert out == [{"role": "user", "content": "hi"}]
    assert hp.receipt_counts()[hp.R_ORPHAN_RESULT] == 1


def test_compactor_legacy_mode_restores_pruning(setting):
    setting("llm_projection_mode", "legacy")
    out = _sanitize_tool_messages([assistant([call("c1")], "text")])
    assert out == [{"role": "assistant", "content": "text"}]


# ── producer -> projection, with the real result builder ─────────────────────

def test_real_tool_result_builder_feeds_certainty_into_the_projection():
    from src.agent_loop import _append_tool_results
    messages = [{"role": "user", "content": "go"}]
    native = [{"id": "n1", "name": "write_file", "arguments": '{"path":"a"}'}]
    _append_tool_results(messages, "", native, [{"status": "partial"}], ["wrote half"],
                         used_native=True, round_num=1,
                         tool_result_records=[{"tool_name": "write_file", "content": "{}",
                                               "result": {"status": "partial"}}])
    assert messages[2]["_effect_status"] == "partial"
    proj = hp.project(messages)
    assert proj.receipts == []
    [exchange] = [e for e in proj.canonical.entries if isinstance(e, hp.Exchange)]
    assert exchange.calls[0].result.certainty == "partial"
    assert "_effect_status" not in json.dumps(proj.messages)
