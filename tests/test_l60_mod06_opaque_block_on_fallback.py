"""MOD-06 — turn-state reconstruction across an intra-call fallback.

Acceptance (docs/spec/v2 backlog, literal): "Cambiar modelo conserva
evidencias y tareas; no interpreta bloques opacos antiguos como texto
ejecutable ni borra adjuntos."

QA-28 already covers "recalcula capacidades ... no simula capacidades
perdidas" (lote 40, green). What QA-28 does not cover, and this lote closes:
the one concrete "opaque block" this codebase has — Gemini's
`thought_signature`, carried as `extra_content` on a native tool_calls entry
(`_append_tool_results`'s own docstring: "Gemini 3 requires the opaque
thought_signature it returned ... or the next request 400s"). Before this
lote, an intra-call fallback away from Gemini handed that block to whatever
provider answered next completely untouched — `src/llm_core.py::
_ollama_normalize_messages` already strips it for native Ollama specifically
("dropped — it is meaningless to Ollama"), but nothing stripped it on the
generic OpenAI-compatible/Anthropic fallback path this lote's file
(`agent_loop.py`) owns.

Gap proof: `grep -n "extra_content" src/agent_loop.py` on the pre-lote file
matches only where `_append_tool_results` WRITES the field — no read site
ever stripped or gated it by destination provider.
"""
from __future__ import annotations

import src.agent_loop as agent_loop


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENAI_URL = "https://api.openai.com/v1"
OLLAMA_URL = "http://127.0.0.1:11434/v1"


def _history_with_opaque_tool_call():
    return [
        {"role": "user", "content": "read config.py"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1_0", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "config.py"}'},
                "extra_content": {"thought_signature": "opaque-gemini-token"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1_0", "content": "x = 1\n"},
    ]


# ── _is_google_endpoint ──────────────────────────────────────────────────

def test_is_google_endpoint_matches_the_real_gemini_host():
    assert agent_loop._is_google_endpoint(GEMINI_URL) is True


def test_is_google_endpoint_is_false_for_other_providers():
    assert agent_loop._is_google_endpoint(OPENAI_URL) is False
    assert agent_loop._is_google_endpoint(OLLAMA_URL) is False


def test_is_google_endpoint_never_raises_on_garbage_input():
    assert agent_loop._is_google_endpoint(None) is False
    assert agent_loop._is_google_endpoint("") is False
    assert agent_loop._is_google_endpoint("not a url at all") is False


# ── _drop_foreign_opaque_tool_extras ────────────────────────────────────

def test_extra_content_is_stripped_when_falling_back_away_from_gemini():
    history = _history_with_opaque_tool_call()
    out = agent_loop._drop_foreign_opaque_tool_extras(history, OPENAI_URL)
    assert "extra_content" not in out[1]["tool_calls"][0]


def test_extra_content_is_stripped_on_fallback_to_ollama_too():
    """Agrees with `llm_core._ollama_normalize_messages`'s own reason for
    dropping it — this is the same policy, applied one layer up."""
    history = _history_with_opaque_tool_call()
    out = agent_loop._drop_foreign_opaque_tool_extras(history, OLLAMA_URL)
    assert "extra_content" not in out[1]["tool_calls"][0]


def test_extra_content_survives_a_fallback_between_two_gemini_endpoints():
    history = _history_with_opaque_tool_call()
    out = agent_loop._drop_foreign_opaque_tool_extras(history, GEMINI_URL)
    assert out is history
    assert out[1]["tool_calls"][0]["extra_content"] == {"thought_signature": "opaque-gemini-token"}


def test_the_original_history_object_is_never_mutated():
    """MOD-06: 'evidencias y tareas' (and the token itself) are not
    destroyed, only kept from leaking to a provider they were never for —
    the ORIGINAL message dict, tool_calls list and tool_call dict must all
    be untouched so a LATER round that falls back back to Gemini still has
    it."""
    history = _history_with_opaque_tool_call()
    original_tc = history[1]["tool_calls"][0]
    agent_loop._drop_foreign_opaque_tool_extras(history, OPENAI_URL)
    assert "extra_content" in history[1]["tool_calls"][0]
    assert history[1]["tool_calls"][0] is original_tc
    assert history[1]["tool_calls"][0]["extra_content"] == {"thought_signature": "opaque-gemini-token"}


def test_messages_with_no_opaque_extras_pass_through_identically():
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = agent_loop._drop_foreign_opaque_tool_extras(history, OPENAI_URL)
    assert out is history


def test_evidence_and_task_content_survive_the_strip_untouched():
    """The strip touches ONLY `extra_content` — the tool call's own name,
    arguments, id, and the tool result content (the 'evidencia') are
    identical before and after, which is the 'no se pierde nada' half of
    MOD-06's acceptance."""
    history = _history_with_opaque_tool_call()
    out = agent_loop._drop_foreign_opaque_tool_extras(history, OPENAI_URL)
    tc = out[1]["tool_calls"][0]
    assert tc["id"] == "call_1_0"
    assert tc["function"] == {"name": "read_file", "arguments": '{"path": "config.py"}'}
    assert out[2]["content"] == "x = 1\n"
    assert out[0]["content"] == "read config.py"


def test_a_non_dict_message_in_the_list_is_passed_through_unchanged():
    """Defensive: some code paths tolerate stray non-dict entries elsewhere
    in this file; this must never crash on one."""
    history = ["not a dict", {"role": "user", "content": "hi"}]
    out = agent_loop._drop_foreign_opaque_tool_extras(history, OPENAI_URL)
    assert out == history


def test_multiple_tool_calls_only_the_opaque_ones_are_touched():
    history = [{
        "role": "assistant", "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"},
             "extra_content": {"thought_signature": "tok"}},
            {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
        ],
    }]
    out = agent_loop._drop_foreign_opaque_tool_extras(history, OPENAI_URL)
    assert "extra_content" not in out[0]["tool_calls"][0]
    assert out[0]["tool_calls"][1] == {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}}
