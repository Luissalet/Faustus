"""CTX-01 — "no se comunica una cifra estimada como conteo exacto".

`budget_for` already labelled each individual input's provenance
(`context_source`, `tools_reserve_source`, `modality_source`) but never gave
a caller a single literal bool to branch on — only prose a UI would have had
to pattern-match. `estimated` closes that: True whenever `input_budget`
rests on ANY non-measured number, False only when every input that fed it
was a real, live-measured figure.

Also pins that the explicit-budget itemised-tightening log line in
`agent_loop.py::_trim_route_request_messages` (already wired since lote 42 —
`tests/test_lote42_ctx01_explicit_budget_itemised.py`) surfaces this same
bool, so an operator reading logs is never told a guessed number was exact.
"""
from __future__ import annotations

import json
import logging

from src.context_budget import budget_for


class TestEstimatedFlag:
    def test_fully_measured_inputs_are_not_estimated(self):
        manifest = {"context_length": 128000, "context_known": True, "context_measured": True}
        result = budget_for(manifest, "text", tool_schema_tokens=2000)
        assert result["context_source"] == "measured"
        assert result["tools_reserve_source"] == "measured"
        assert result["estimated"] is False

    def test_known_default_context_is_estimated(self):
        manifest = {"context_length": 200000, "context_known": True, "context_measured": False}
        result = budget_for(manifest, "text", tool_schema_tokens=2000)
        assert result["context_source"] == "known_default"
        assert result["estimated"] is True

    def test_unknown_context_is_estimated(self):
        manifest = {"context_length": 0, "context_known": False}
        result = budget_for(manifest, "text")
        assert result["context_source"] == "unknown"
        assert result["estimated"] is True

    def test_guessed_tools_reserve_alone_makes_it_estimated(self):
        manifest = {"context_length": 128000, "context_known": True, "context_measured": True}
        result = budget_for(manifest, "text")  # no tool_schema_tokens passed
        assert result["context_source"] == "measured"
        assert result["tools_reserve_source"] == "estimated"
        assert result["estimated"] is True

    def test_a_documented_image_cost_is_estimated_even_with_a_measured_window(self):
        """A large attachment must not silently pass as an exact count: the
        per-image figure is a published flat estimate, not derived from THIS
        attachment's real pixel dimensions — same acceptance clause as "un
        adjunto grande no desplaza ... los criterios"."""
        manifest = {
            "context_length": 128000, "context_known": True, "context_measured": True,
            "provider": "openai",
        }
        result = budget_for(manifest, "image", tool_schema_tokens=2000)
        assert result["modality_unit_cost"] == 1200
        assert result["estimated"] is True

    def test_an_undocumented_modality_cost_does_not_by_itself_force_estimated(self):
        """`modality_unit_cost is None` means "not counted at all", not "an
        estimate was used" — those are different honesty claims, so this
        alone must not flip the flag when everything else was measured."""
        manifest = {
            "context_length": 128000, "context_known": True, "context_measured": True,
            "provider": "ollama",
        }
        result = budget_for(manifest, "image", tool_schema_tokens=2000)
        assert result["modality_unit_cost"] is None
        assert result["estimated"] is False

    def test_estimated_is_always_a_plain_bool(self):
        for manifest in (None, {}, {"context_length": "bad"}):
            result = budget_for(manifest, "text")
            assert result["estimated"] in (True, False)
            assert isinstance(result["estimated"], bool)


def _agent_loop_module():
    import src.agent_loop as agent_loop
    return agent_loop


class TestTighteningLogSurfacesTheFlag:
    def test_log_line_reports_estimated_true_for_a_known_default_window(self, monkeypatch, caplog):
        """Integration point this lote owns: `_trim_route_request_messages`
        (explicit-cap branch, wired since lote 42) must not just tighten the
        number silently — its own log line names whether the tightened
        figure itself is estimated, using `budget_for`'s new literal flag
        rather than re-deriving it."""
        agent_loop = _agent_loop_module()

        monkeypatch.setattr(agent_loop, "get_setting",
                             lambda key, default=None: 99000 if key == "agent_input_token_budget" else default)
        monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
        monkeypatch.setattr(agent_loop, "estimate_tokens", lambda messages: len(messages) * 10)
        monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
        monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *a, **k: (True, False, False))

        def fake_build(messages, model, *args, **kwargs):
            return (
                [{"role": "system", "content": "route prompt", "_agent_injected": "prompt"}]
                + list(messages),
                [],
            )
        monkeypatch.setattr(agent_loop, "_build_system_prompt", fake_build)

        import src.model_context as model_context
        import src.context_compactor as context_compactor
        # A known-but-not-live-measured window (budget_context_for_model's
        # own contract has no "measured" bit -- see the _manifest built in
        # _trim_route_request_messages itself, context_measured=False always).
        monkeypatch.setattr(model_context, "budget_context_for_model", lambda *a, **kw: 100_000)
        monkeypatch.setattr(context_compactor, "trim_for_context", lambda messages, budget, reserve_tokens=0: list(messages))

        async def fake_stream(candidates, messages, **kwargs):
            yield f'data: {json.dumps({"delta": "done"})}\n\n'
            yield "data: [DONE]\n\n"
        monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

        async def go():
            return [c async for c in agent_loop.stream_agent_loop(
                "https://selected.example/v1", "selected-model",
                [{"role": "user", "content": "hello"}],
                max_rounds=1, relevant_tools={"bash"}, fallbacks=[],
                harness_options={"input_token_budget": "99000"},
                _is_teacher_run=True,
            )]

        import asyncio
        with caplog.at_level(logging.INFO, logger="src.agent_loop"):
            asyncio.run(go())

        tightening_lines = [r.message for r in caplog.records if "tightened by itemised reserves" in r.message]
        assert tightening_lines, "expected the itemised-tightening log line to fire"
        assert "estimated=True" in tightening_lines[0]
