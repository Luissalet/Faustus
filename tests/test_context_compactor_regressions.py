"""Regression coverage for the four chained context-compaction bugs.

Together they formed a destructive cycle: the older half of a chat was
summarized, the original messages were DELETED FROM THE DATABASE (some of them
messages the model was being shown at that very moment), and the summary that
replaced them was then thrown away by the trim that runs immediately
afterwards. The conversation was lost and nothing was left in its place.

  1. ``_update_session_history`` mapped a prompt index onto the transcript by
     adding the number of system messages. The prompt is
     ``preface + session.get_context_messages()``: the preface contributes
     system messages *and* ``role: "user"`` memory/RAG/web blocks plus a
     date/time message, and the history view filters slash chatter out — so the
     offset was wrong in both directions and deleted live messages.
  2. The compaction summary landed in ``extra_system`` with the same priority
     as a memory blob and lost to it in ``trim_for_context``.
  3. ``trim_for_context`` pinned the last ten turns unconditionally, returned
     prompts at ~2x the window, and "compensated" by cutting the user's own
     message with a notice claiming it had been too large to paste.
  4. The stream guard compared the current request with the prompt's last user
     turn by substring. A notice spliced into the middle of a shortened message
     defeats that, so the guard appended the whole original message again.
"""

import asyncio
import importlib

import pytest

import src.context_compactor as cc
from src.context_compactor import (
    annotate_history_positions,
    maybe_compact,
    trim_for_context,
)
from src.model_context import estimate_tokens

core_models = importlib.import_module("core.models")


# --------------------------------------------------------------------------- #
# Fakes that mirror the real prompt shape without the app stack
# --------------------------------------------------------------------------- #

class _Msg:
    """Stand-in for core.models.ChatMessage (same attribute contract)."""

    def __init__(self, role, content, metadata=None):
        self.role = role
        self.content = content
        self.metadata = metadata or {}

    def to_dict(self):
        out = {"role": self.role, "content": self.content}
        if self.metadata:
            out["metadata"] = self.metadata
        return out

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<{self.role}:{str(self.content)[:16]}>"


class _Session:
    """Mirrors Session.get_context_messages(): filters slash rows, keeps order."""

    def __init__(self, history, session_id=None):
        self.history = list(history)
        self.id = session_id

    def get_context_messages(self):
        return [
            m.to_dict()
            for m in self.history
            if (m.metadata or {}).get("source") != "slash"
        ]


def _preface():
    """What chat_processor.build_context_preface actually returns.

    Two system messages, plus a memory/RAG block that is ``role: "user"``
    (src/prompt_security.untrusted_context_message) — the shape that made
    "count the system messages" wrong.
    """
    return [
        {"role": "system", "content": "PRESET SYSTEM PROMPT " + "p" * 2500},
        {"role": "system", "content": "UNTRUSTED CONTEXT POLICY " + "q" * 2500},
        {
            "role": "user",
            "content": "saved memory: pinned context " + "m" * 2500,
            "metadata": {"trusted": False, "source": "saved memory"},
        },
    ]


@pytest.fixture
def compactor(monkeypatch):
    """Hermetic compaction: no network, no endpoint resolution, no DB."""
    monkeypatch.setattr(cc, "ChatMessage", _Msg)
    monkeypatch.setattr(cc, "resolve_endpoint", lambda which, owner=None: (None, None, None))
    monkeypatch.setattr(core_models, "get_session_manager_instance", lambda: None, raising=False)

    def _run(session, messages, *, context_length=8192, summary="SUMMARY TEXT"):
        monkeypatch.setattr(cc, "get_context_length", lambda url, model: context_length)

        async def _fake_summary(*args, **kwargs):
            return summary

        monkeypatch.setattr(cc, "llm_call_async", _fake_summary)
        return asyncio.run(
            maybe_compact(session, "http://local/v1", "model", list(messages), {})
        )

    return _run


def _conversation(turns=10, filler=2500):
    return [
        _Msg("user" if i % 2 == 0 else "assistant", f"HIST-{i} " + "z" * filler)
        for i in range(turns)
    ]


# --------------------------------------------------------------------------- #
# BUG 1 — compaction deleted transcript rows it had no mapping for
# --------------------------------------------------------------------------- #

class TestCompactionDoesNotDeleteLiveMessages:
    def test_never_deletes_a_message_the_model_is_being_shown(self, compactor):
        history = _conversation()
        session = _Session(history)
        context_messages = session.get_context_messages()
        annotate_history_positions(session, context_messages)
        messages = _preface() + context_messages

        compacted, _ctx, was_compacted = compactor(session, messages)

        assert was_compacted is True
        shown = {
            str(m.get("content"))
            for m in compacted
            if m.get("role") in ("user", "assistant")
        }
        surviving = {str(m.content) for m in session.history}
        still_shown_but_deleted = sorted(
            text for text in shown if text.startswith("HIST-") and text not in surviving
        )
        assert still_shown_but_deleted == []

    def test_summarized_rows_are_the_ones_removed(self, compactor):
        history = _conversation()
        session = _Session(history)
        context_messages = session.get_context_messages()
        annotate_history_positions(session, context_messages)
        messages = _preface() + context_messages

        compacted, _ctx, was_compacted = compactor(session, messages)

        assert was_compacted is True
        # Compaction still does its job: the older half is gone from the
        # transcript and a summary row stands in its place.
        assert len(session.history) < len(history)
        assert any(
            isinstance(getattr(row, "content", None), str)
            and row.content.startswith("[Conversation summary]")
            for row in session.history
        )
        removed = [m for m in history if m not in session.history]
        assert removed, "compaction must still compact when the mapping is provable"
        # Every removed row was part of the older half fed to the summarizer,
        # and none of them is still in the prompt.
        prompt_texts = {str(m.get("content")) for m in compacted}
        assert all(str(m.content) not in prompt_texts for m in removed)
        # The tail of the conversation is untouched.
        assert history[-1] in session.history

    def test_slash_rows_do_not_shift_the_mapping(self, compactor):
        # get_context_messages() filters source=="slash", so history positions
        # and prompt positions drift apart by however many are interleaved.
        history = _conversation()
        history.insert(3, _Msg("user", "/setup copilot", {"source": "slash"}))
        history.insert(4, _Msg("assistant", "Starting sign-in...", {"source": "slash"}))
        session = _Session(history)
        context_messages = session.get_context_messages()
        annotate_history_positions(session, context_messages)
        messages = _preface() + context_messages

        compacted, _ctx, _was = compactor(session, messages)

        shown = {
            str(m.get("content"))
            for m in compacted
            if m.get("role") in ("user", "assistant")
        }
        surviving = {str(m.content) for m in session.history}
        assert all(text in surviving for text in shown if text.startswith("HIST-"))

    def test_history_untouched_without_a_provable_mapping(self, compactor):
        # No stamps (e.g. an incognito transcript, or a prompt assembled
        # elsewhere): compact the prompt, never guess at the transcript.
        history = _conversation()
        session = _Session(history)
        messages = _preface() + session.get_context_messages()

        _compacted, _ctx, was_compacted = compactor(session, messages)

        assert was_compacted is True
        assert session.history == history

    def test_refuses_to_persist_when_the_transcript_moved(self, compactor, monkeypatch):
        history = _conversation()
        session = _Session(history)
        context_messages = session.get_context_messages()
        annotate_history_positions(session, context_messages)
        messages = _preface() + context_messages

        state = {}
        monkeypatch.setattr(cc, "get_context_length", lambda url, model: 8192)

        async def _fake_summary(*args, **kwargs):
            return "deferred summary"

        monkeypatch.setattr(cc, "llm_call_async", _fake_summary)
        _out, _ctx, was_compacted = asyncio.run(
            maybe_compact(
                session, "http://local/v1", "model", list(messages), {},
                persist=False, compaction_state=state,
            )
        )
        assert was_compacted is True

        # The user edited/deleted a message before the route committed.
        session.history = [_Msg("user", "a completely different opening")] + history[1:]
        before = list(session.history)
        assert cc.apply_compaction_state(session, state) is True
        assert session.history == before

    def test_deletion_goes_through_the_session_manager(self, compactor, monkeypatch):
        history = _conversation()
        session = _Session(history, session_id="s1")
        context_messages = session.get_context_messages()
        annotate_history_positions(session, context_messages)
        messages = _preface() + context_messages

        replaced = {}

        class _Manager:
            def replace_messages(self, session_id, new_history):
                replaced["id"] = session_id
                replaced["history"] = list(new_history)
                return True

        monkeypatch.setattr(
            core_models, "get_session_manager_instance", lambda: _Manager(), raising=False
        )
        compacted, _ctx, _was = compactor(session, messages)

        assert replaced["id"] == "s1"
        shown = {
            str(m.get("content"))
            for m in compacted
            if m.get("role") in ("user", "assistant")
        }
        persisted = {str(getattr(m, "content", "")) for m in replaced["history"]}
        assert all(text in persisted for text in shown if text.startswith("HIST-"))


class TestAnnotateHistoryPositions:
    def test_indices_account_for_filtered_rows(self):
        history = [
            _Msg("user", "one"),
            _Msg("user", "/setup", {"source": "slash"}),
            _Msg("assistant", "two"),
        ]
        session = _Session(history)
        context_messages = session.get_context_messages()

        assert annotate_history_positions(session, context_messages) == 2
        assert [m[cc.HISTORY_INDEX_KEY] for m in context_messages] == [0, 2]

    def test_returns_zero_for_a_session_without_history(self):
        class _NoHistory:
            pass

        assert annotate_history_positions(_NoHistory(), [{"role": "user", "content": "x"}]) == 0

    def test_stamps_nothing_when_the_prompt_is_not_a_subsequence(self):
        session = _Session([_Msg("user", "one")])
        messages = [{"role": "user", "content": "not from this history"}]
        assert annotate_history_positions(session, messages) == 0
        assert cc.HISTORY_INDEX_KEY not in messages[0]


# --------------------------------------------------------------------------- #
# BUG 2 — the trim threw away the compaction summary
# --------------------------------------------------------------------------- #

class TestCompactionSummarySurvivesTrimming:
    def test_summary_outranks_memory_and_rag_blobs(self, compactor):
        messages = [
            {"role": "system", "content": "You are Faustus."},
            {"role": "system", "content": "Saved memories:\n" + "the project is faustus. " * 700},
        ]
        for i in range(14):
            messages.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"turn{i}: " + "content " * 350,
            })
        messages.append({"role": "user", "content": "so what do I run now?"})

        compacted, context_length, was_compacted = compactor(
            None, messages, context_length=4096,
            summary="### User Goal\nFIX THE DEPLOY SCRIPT\n" + "detail " * 60,
        )
        assert was_compacted is True
        assert any("Conversation summary" in str(m.get("content")) for m in compacted)

        trimmed = trim_for_context(compacted, context_length)
        assert any("Conversation summary" in str(m.get("content")) for m in trimmed)
        assert estimate_tokens(trimmed) <= context_length - 512

    def test_summary_row_loaded_from_history_is_essential(self):
        messages = [
            {"role": "system", "content": "You are Faustus."},
            {"role": "system", "content": "RAG-BLOB " + "r" * 9000},
            {
                "role": "system",
                "content": "[Conversation summary]\nSUMMARY-MARKER " + "s" * 400,
                "metadata": {"compacted": True, "summarized_count": 8},
            },
            {"role": "user", "content": "carry on"},
        ]
        trimmed = trim_for_context(messages, context_length=2048, reserve_tokens=512)
        joined = "\n".join(str(m.get("content", "")) for m in trimmed)
        assert "SUMMARY-MARKER" in joined
        assert "RAG-BLOB" not in joined


# --------------------------------------------------------------------------- #
# BUG 3 — trim_for_context returned over budget and mutilated the current turn
# --------------------------------------------------------------------------- #

def _corpus_single_giant_paste():
    return [
        {"role": "system", "content": "You are Faustus."},
        {"role": "user", "content": "def f():\n    return 1\n" * 3000},
    ]


def _corpus_many_turns():
    msgs = [{"role": "system", "content": "You are Faustus."}]
    for i in range(60):
        msgs.append({"role": "user", "content": f"q{i} " + "x" * 900})
        msgs.append({"role": "assistant", "content": f"a{i} " + "y" * 1100})
    msgs.append({"role": "user", "content": "which one has the bug?"})
    return msgs


def _corpus_bulky_recent_window():
    msgs = [{"role": "system", "content": "You are Faustus."}]
    for i in range(30):
        msgs.append({"role": "user", "content": f"old-{i}"})
        msgs.append({"role": "assistant", "content": f"ans-{i}"})
    for _ in range(9):
        msgs.append({"role": "assistant", "content": "FILE DUMP " + "d" * 9000})
    msgs.append({"role": "user", "content": "explain this traceback:\n" + "TRACEBACK LINE\n" * 20})
    return msgs


def _corpus_huge_system_prompt():
    return [
        {"role": "system", "content": "PERSONA " + "s" * 120000},
        {"role": "system", "content": "MEMORY " + "m" * 40000},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "and now the real question " + "?" * 3000},
    ]


def _corpus_primer_and_protected_document():
    msgs = [
        {"role": "system", "content": "You are Faustus."},
        {"role": "system", "content": "RETRIEVED " + "r" * 20000},
        {
            "role": "system",
            "content": "=== REPORT ===\n" + "z" * 30000,
            "metadata": {"research_spinoff_from": "rp-1"},
        },
        {"role": "system", "content": "ACTIVE DOC " + "d" * 50000, "_protected": True},
    ]
    for i in range(20):
        msgs.append({"role": "user", "content": f"q{i} " + "u" * 800})
        msgs.append({"role": "assistant", "content": f"a{i} " + "v" * 800})
    msgs.append({"role": "user", "content": "summarise the report please"})
    return msgs


def _corpus_tool_calls():
    msgs = [{"role": "system", "content": "You are Faustus."}]
    for i in range(12):
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "create_document", "arguments": "x" * 8000},
            }],
        })
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "result " + "t" * 4000})
    msgs.append({"role": "user", "content": "what did you find?"})
    return msgs


def _corpus_multimodal():
    return [
        {"role": "system", "content": "You are Faustus."},
        {"role": "user", "content": [
            {"type": "text", "text": "look at this: " + "L" * 40000},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
        {"role": "assistant", "content": "ok " + "o" * 20000},
        {"role": "user", "content": [{"type": "text", "text": "and now? " + "N" * 30000}]},
    ]


def _corpus_everything():
    msgs = _corpus_primer_and_protected_document()[:4]
    msgs.append({
        "role": "system",
        "content": "[Conversation summary]\n" + "c" * 6000,
        "metadata": {"compacted": True},
    })
    msgs.extend(_corpus_many_turns()[1:])
    msgs.extend(_corpus_tool_calls()[1:-1])
    msgs.append({"role": "user", "content": "final question " + "f" * 5000})
    return msgs


CORPORA = {
    "single_giant_paste": _corpus_single_giant_paste,
    "many_turns": _corpus_many_turns,
    "bulky_recent_window": _corpus_bulky_recent_window,
    "huge_system_prompt": _corpus_huge_system_prompt,
    "primer_and_protected_document": _corpus_primer_and_protected_document,
    "tool_calls": _corpus_tool_calls,
    "multimodal": _corpus_multimodal,
    "everything": _corpus_everything,
}


@pytest.mark.parametrize("corpus_name", sorted(CORPORA))
@pytest.mark.parametrize("context_length,reserve", [
    (1024, 512), (2048, 512), (4096, 512), (8192, 512), (8192, 256), (32768, 512),
])
def test_trim_for_context_never_exceeds_the_budget(corpus_name, context_length, reserve):
    """The hard invariant. Nothing downstream re-trims (src/llm_core.py does
    not), so whatever this returns is what the model is asked to accept."""
    messages = CORPORA[corpus_name]()
    budget = context_length - reserve

    trimmed = trim_for_context(messages, context_length, reserve_tokens=reserve)

    used = estimate_tokens(trimmed)
    assert used <= budget, (
        f"{corpus_name} @ ctx={context_length}/reserve={reserve}: "
        f"returned {used} tokens for a {budget} budget ({used / max(budget, 1):.1f}x)"
    )
    assert trimmed, "the prompt must never be emptied at these budgets"


def test_recent_window_yields_before_the_user_message_is_mutilated():
    messages = _corpus_bulky_recent_window()
    question = messages[-1]["content"]

    trimmed = trim_for_context(messages, 8192)

    assert estimate_tokens(trimmed) <= 8192 - 512
    assert trimmed[-1]["role"] == "user"
    assert trimmed[-1]["content"] == question
    assert "pasted message was too large" not in trimmed[-1]["content"]


def test_no_oversize_notice_when_the_message_was_not_oversized():
    """The notice claims the user's paste was too big for the window. It must
    never be the excuse for a prefix that did not fit."""
    messages = [{"role": "system", "content": "You are Faustus."}]
    for i in range(5):
        messages.append({"role": "user", "content": f"q{i}\n" + "line of code\n" * 420})
        messages.append({"role": "assistant", "content": f"a{i}\n" + "fixed line\n" * 480})
    question = "Now tell me in one sentence which of those five files has the bug."
    messages.append({"role": "user", "content": question})

    trimmed = trim_for_context(messages, 8192)

    assert estimate_tokens(trimmed) <= 8192 - 512
    assert trimmed[-1]["content"] == question
    assert "pasted message was too large" not in trimmed[-1]["content"]


def test_genuinely_oversized_paste_still_says_so():
    huge = "A" * 40000
    trimmed = trim_for_context(
        [{"role": "system", "content": "You are Faustus."}, {"role": "user", "content": huge}],
        2048,
    )
    assert "pasted message was too large" in trimmed[-1]["content"]
    assert estimate_tokens(trimmed) <= 2048 - 512


# --------------------------------------------------------------------------- #
# BUG 4 — the stream guard re-appended the whole message
# --------------------------------------------------------------------------- #

@pytest.fixture
def guard():
    chat_routes = importlib.import_module("routes.chat_routes")
    return chat_routes._ensure_current_request_is_latest_user


def test_guard_recognises_a_shortened_current_message(guard):
    paste = "def f():\n    return 1\n" * 1200
    messages = [
        {"role": "system", "content": "You are Faustus."},
        {"role": "user", "content": paste},
    ]
    trimmed = trim_for_context(messages, 8192)
    assert trimmed[-1]["content"] != paste, "precondition: the trimmer shortened it"

    repaired = guard(trimmed, paste, 8192)

    assert sum(1 for m in repaired if m["role"] == "user") == 1
    assert estimate_tokens(repaired) <= 8192 - 512


def test_guard_repair_stays_within_the_budget(guard):
    """Even when the guard is right to fire, it must not hand back a prompt
    over the window it was given."""
    messages = [
        {"role": "system", "content": "You are Faustus."},
        {"role": "user", "content": "an entirely unrelated older turn " + "u" * 20000},
    ]
    current = "the request this stream was actually created for " + "c" * 20000

    repaired = guard(messages, current, 8192)

    assert estimate_tokens(repaired) <= 8192 - 512


def test_guard_still_repairs_a_genuine_mismatch(guard):
    messages = [
        {"role": "system", "content": "You are Faustus."},
        {"role": "user", "content": "a different conversation entirely"},
    ]
    repaired = guard(messages, "what is the weather in Madrid?", 8192)

    assert repaired[-1] == {"role": "user", "content": "what is the weather in Madrid?"}


def test_guard_is_a_no_op_for_a_matching_message(guard):
    messages = [
        {"role": "system", "content": "You are Faustus."},
        {"role": "user", "content": "hello there"},
    ]
    assert guard(messages, "hello there", 8192) is messages
