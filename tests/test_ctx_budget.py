"""CTX-01 — real per-model, per-modality budget (docs/spec/v2 backlog).

`budget_for` must never hand back a 0 or invented number: an unknown context
window falls back to a documented conservative default (never 0), and an
unknown modality cost comes back as `None` with a `source` that says why,
never a silent 0 that would read as "this costs nothing".
"""

from src.context_budget import budget_for, modality_unit_cost, DEFAULT_BUDGET


class TestModalityUnitCost:
    def test_text_has_no_per_unit_cost(self):
        cost, source = modality_unit_cost("openai", "text")
        assert cost is None
        assert "per character" in source

    def test_documented_provider_image_cost_is_real_and_sourced(self):
        cost, source = modality_unit_cost("openai", "image")
        assert cost == 1200  # matches src.model_context.IMAGE_BLOCK_TOKENS
        assert "openai" in source

    def test_google_image_cost_differs_from_openai(self):
        google_cost, _ = modality_unit_cost("google", "image")
        openai_cost, _ = modality_unit_cost("openai", "image")
        assert google_cost == 258
        assert google_cost != openai_cost

    def test_undocumented_provider_is_unknown_not_zero(self):
        """A local/self-hosted VLM (ollama, llama.cpp, vllm...) has no
        published per-image token cost in Faustus's table. The rule is:
        never 0, never a borrowed number — `None` with a visible reason."""
        cost, source = modality_unit_cost("ollama", "image")
        assert cost is None
        assert cost != 0
        assert "no documented" in source

    def test_audio_and_video_are_unknown(self):
        for modality in ("audio", "video"):
            cost, source = modality_unit_cost("openai", modality)
            assert cost is None
            assert "no documented" in source


class TestBudgetFor:
    def test_measured_window_is_labelled_measured(self):
        manifest = {"context_length": 128000, "context_known": True, "context_measured": True}
        result = budget_for(manifest, "text")
        assert result["context_source"] == "measured"
        assert result["context_length"] == 128000

    def test_known_table_window_is_not_claimed_measured(self):
        manifest = {"context_length": 200000, "context_known": True, "context_measured": False}
        result = budget_for(manifest, "text")
        assert result["context_source"] == "known_default"

    def test_unknown_window_never_reports_zero_and_says_so(self):
        manifest = {"context_length": 0, "context_known": False}
        result = budget_for(manifest, "text")
        assert result["context_source"] == "unknown"
        assert result["context_length"] == DEFAULT_BUDGET
        assert result["context_length"] > 0
        assert result["input_budget"] > 0
        assert any("unknown" in w for w in result["warnings"])

    def test_input_budget_is_never_zero_or_negative_even_for_a_tiny_window(self):
        manifest = {"context_length": 512, "context_known": True, "context_measured": True}
        result = budget_for(manifest, "text", tool_schema_tokens=2000)
        assert result["input_budget"] >= 1
        assert result["reserve_response"] >= 1
        assert result["reserve_tools"] >= 1

    def test_reserves_come_out_of_the_window(self):
        manifest = {"context_length": 100000, "context_known": True, "context_measured": True}
        result = budget_for(manifest, "text", tool_schema_tokens=5000, response_reserve=2000)
        assert result["reserve_tools"] == 5000
        assert result["reserve_response"] == 2000
        assert result["input_budget"] == 100000 - 5000 - 2000

    def test_measured_tool_schema_tokens_are_used_exactly(self):
        manifest = {"context_length": 32768, "context_known": True, "context_measured": True}
        result = budget_for(manifest, "text", tool_schema_tokens=1234)
        assert result["reserve_tools"] == 1234
        assert result["tools_reserve_source"] == "measured"

    def test_missing_tool_schema_tokens_falls_back_to_an_estimate_not_zero(self):
        manifest = {"context_length": 32768, "context_known": True, "context_measured": True}
        result = budget_for(manifest, "text")
        assert result["reserve_tools"] > 0
        assert result["tools_reserve_source"] == "estimated"

    def test_image_modality_carries_a_real_cost_for_a_known_provider(self):
        manifest = {
            "context_length": 128000, "context_known": True, "context_measured": True,
            "provider": "openai",
        }
        result = budget_for(manifest, "image")
        assert result["modality_unit_cost"] == 1200
        assert result["modality"] == "image"

    def test_malformed_manifest_never_crashes_and_never_returns_zero_budget(self):
        for manifest in (None, {}, {"context_length": "not-a-number"}, {"context_length": -5}):
            result = budget_for(manifest, "text")
            assert result["context_length"] > 0
            assert result["input_budget"] > 0
