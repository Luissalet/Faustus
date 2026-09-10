"""CTX-02 — compression with semantic integrity (docs/spec/v2 backlog).

`compact_with_integrity` is a deterministic, model-free compaction path:
identifiers, pasted code, ask_user decisions and the current user message
must never be paraphrased away, whatever an LLM-based summarizer would have
done with them.
"""

from src.context_compactor import (
    compact_with_integrity,
    extract_protected_strings,
)


def _identifiers_message():
    paths = [f"src/module_{i}.py" for i in range(12)]
    hashes = ["a1b2c3d", "deadbeefcafebabe", "0123abc"]
    text = (
        "Working on files: " + ", ".join(paths) + ". Hashes seen: "
        + ", ".join(hashes) + "."
    )
    return text, paths + hashes


def _filler_messages(n, *, before_last=True):
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"filler question {i}"})
        msgs.append({"role": "assistant", "content": f"filler answer {i}"})
    return msgs


class TestExtractProtectedStrings:
    def test_paths_urls_hashes_and_units_survive_extraction(self):
        text = (
            "See src/context_budget.py and https://example.com/docs, "
            "hash a1b2c3d, took 42ms and used 300MB."
        )
        found = extract_protected_strings(text)
        assert "src/context_budget.py" in found
        assert "https://example.com/docs" in found
        assert "a1b2c3d" in found
        assert any("42ms" in f for f in found)
        assert any("300MB" in f for f in found)

    def test_plain_numbers_are_not_misread_as_hashes(self):
        # All-digit, no a-f letter — must not be treated as a hex hash.
        found = extract_protected_strings("order 1234567 shipped")
        assert "1234567" not in found

    def test_empty_and_non_string_input_returns_empty(self):
        assert extract_protected_strings("") == []
        assert extract_protected_strings(None) == []


class TestCompactWithIntegrity:
    def test_twelve_paths_and_three_hashes_survive_compaction_intact(self):
        """The exact scenario the lote asks for: a message with 12 paths and
        3 hashes must survive compaction with all 15 strings intact."""
        identifiers_text, identifiers = _identifiers_message()
        assert len(identifiers) == 15

        messages = [{"role": "system", "content": "system prompt"}]
        messages += _filler_messages(1)
        messages.append({"role": "user", "content": identifiers_text})
        messages.append({"role": "assistant", "content": "noted"})
        messages += _filler_messages(4)  # pushes the identifiers message out of the recent window
        messages.append({"role": "user", "content": "final question"})

        new_messages, evidence_refs = compact_with_integrity(
            messages, session_id="sess-1", keep_recent=4
        )

        blob = "\n".join(str(m.get("content") or "") for m in new_messages)
        missing = [s for s in identifiers if s not in blob]
        assert missing == [], f"identifiers lost during compaction: {missing}"
        # And it must actually have compacted something (fewer messages).
        assert len(new_messages) < len(messages)
        assert evidence_refs and evidence_refs[0]["evidence_id"]

    def test_marker_carries_the_required_format(self):
        identifiers_text, _ = _identifiers_message()
        messages = [{"role": "user", "content": identifiers_text}]
        messages.append({"role": "assistant", "content": "ok"})
        messages += _filler_messages(4)
        messages.append({"role": "user", "content": "final question"})

        new_messages, _ = compact_with_integrity(messages, session_id="s", keep_recent=4)
        markers = [m for m in new_messages if "[compactado:" in str(m.get("content") or "")]
        assert markers, "expected a [compactado: ...] marker message"
        marker_text = markers[0]["content"]
        assert " mensajes, " in marker_text
        assert " tokens → " in marker_text

    def test_evidence_ref_is_attached_and_indexed(self):
        from src.context_ledger import evidence_index

        identifiers_text, _ = _identifiers_message()
        messages = [{"role": "user", "content": identifiers_text}]
        messages.append({"role": "assistant", "content": "ok"})
        messages += _filler_messages(4)
        messages.append({"role": "user", "content": "final question"})

        new_messages, evidence_refs = compact_with_integrity(messages, session_id="s2", keep_recent=4)
        assert evidence_refs
        idx = evidence_index(new_messages)
        assert evidence_refs[0]["evidence_id"] in idx

    def test_pasted_code_block_is_never_summarized(self):
        code_msg = {"role": "user", "content": "```python\ndef unique_fn_marker():\n    return 42\n```"}
        messages = [code_msg]
        messages.append({"role": "assistant", "content": "ok"})
        messages += _filler_messages(4)
        messages.append({"role": "user", "content": "final question"})

        new_messages, _ = compact_with_integrity(messages, session_id="s3", keep_recent=4)
        assert code_msg in new_messages

    def test_ask_user_decision_is_never_summarized(self):
        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "ask_user", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "asked"},
        ]
        decision_msg = {"role": "user", "content": "I choose option B"}
        messages.append(decision_msg)
        messages.append({"role": "assistant", "content": "ok"})
        messages += _filler_messages(4)
        messages.append({"role": "user", "content": "final question"})

        new_messages, _ = compact_with_integrity(messages, session_id="s4", keep_recent=4)
        assert decision_msg in new_messages

    def test_last_user_message_always_survives_verbatim(self):
        messages = [{"role": "user", "content": "old"}]
        messages += _filler_messages(4)
        last = {"role": "user", "content": "the real current question"}
        messages.append(last)

        new_messages, _ = compact_with_integrity(messages, session_id="s5", keep_recent=1)
        assert last in new_messages

    def test_small_conversation_is_a_no_op(self):
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        new_messages, evidence_refs = compact_with_integrity(messages, keep_recent=4)
        assert new_messages == messages
        assert evidence_refs == []

    def test_does_not_mutate_the_input_list(self):
        identifiers_text, _ = _identifiers_message()
        messages = [{"role": "user", "content": identifiers_text}]
        messages.append({"role": "assistant", "content": "ok"})
        messages += _filler_messages(4)
        messages.append({"role": "user", "content": "final question"})
        original_len = len(messages)
        original_first = messages[0]

        compact_with_integrity(messages, session_id="s6", keep_recent=4)
        assert len(messages) == original_len
        assert messages[0] is original_first
