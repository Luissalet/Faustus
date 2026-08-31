"""Deep Research profiles matched to the hardware (FAUSTUS).

The failure this exists for is quiet: on a 12 GB card the shipped defaults run
six extractions at once against one GPU, every one of them blows a 90-second
timeout, and the run finishes with no content — which reads like the web was
empty rather than like a settings problem. Tiers here are the fix; blockers are
the other half, because no amount of tuning helps when nothing answers a query.
"""

from src.research_presets import (RESEARCH_KEYS, apply_patch, blockers,
                                  recommend, tier_for)


class TestTiers:
    def test_a_12gb_card_lands_in_mid(self):
        """The machine this fork is developed on — an RTX 4070 Ti."""
        tier = tier_for(12.0)
        assert tier["name"] == "mid"
        assert tier["patch"]["research_extraction_concurrency"] == 3

    def test_boundaries_are_not_ambiguous(self):
        assert tier_for(9.9)["name"] == "tight"
        assert tier_for(10.0)["name"] == "mid"
        assert tier_for(16.9)["name"] == "mid"
        assert tier_for(17.0)["name"] == "roomy"
        assert tier_for(33.0)["name"] == "big"
        assert tier_for(512.0)["name"] == "big"

    def test_unknown_hardware_is_conservative_and_says_so(self):
        for value in (None, 0, -1):
            tier = tier_for(value)
            assert tier["name"] == "tight"
            assert "unknown" in tier["note"].lower()

    def test_every_tier_sets_every_key_it_owns(self):
        for vram in (4, 12, 24, 96):
            assert set(tier_for(vram)["patch"]) == set(RESEARCH_KEYS)

    def test_smaller_cards_get_fewer_extractions_and_longer_timeouts(self):
        small, large = tier_for(8), tier_for(48)
        assert (small["patch"]["research_extraction_concurrency"]
                < large["patch"]["research_extraction_concurrency"])
        assert (small["patch"]["research_extraction_timeout_seconds"]
                >= large["patch"]["research_extraction_timeout_seconds"])


class TestBlockers:
    def test_searxng_selected_but_unreachable(self):
        found = blockers({"search_provider": "searxng"}, searxng_ok=False)
        assert found[0]["key"] == "searxng_unreachable"
        assert found[0]["fix"] == {"search_provider": "duckduckgo"}

    def test_a_reachable_or_unprobed_searxng_is_not_a_blocker(self):
        for probe in (True, None):
            assert not [b for b in blockers({"search_provider": "searxng",
                                             "research_model": "m"},
                                            searxng_ok=probe)
                        if b["key"] == "searxng_unreachable"]

    def test_a_keyed_provider_without_its_key(self):
        found = blockers({"search_provider": "brave", "research_model": "m"})
        assert found[0]["key"] == "missing_api_key"
        found = blockers({"search_provider": "brave", "brave_api_key": "k",
                          "research_model": "m"})
        assert found == []

    def test_search_turned_off_entirely(self):
        found = blockers({"search_provider": "disabled", "research_model": "m"})
        assert found[0]["key"] == "search_disabled"

    def test_no_model_anywhere(self):
        found = blockers({"search_provider": "duckduckgo"})
        assert [b["key"] for b in found] == ["no_model"]
        assert blockers({"search_provider": "duckduckgo",
                         "default_model": "qwen"}) == []

    def test_a_healthy_setup_reports_nothing(self):
        assert blockers({"search_provider": "duckduckgo",
                         "research_model": "qwen"}, searxng_ok=True) == []


class TestRecommend:
    def test_it_reports_only_what_would_change(self):
        settings = dict(tier_for(12.0)["patch"])
        settings.update({"research_model": "m", "search_provider": "duckduckgo"})
        rec = recommend(settings, vram_gb=12.0)
        assert rec["changes"] == [] and rec["already_applied"] is True

        settings["research_extraction_concurrency"] = 8
        rec = recommend(settings, vram_gb=12.0)
        assert [c["key"] for c in rec["changes"]] == ["research_extraction_concurrency"]
        assert rec["changes"][0]["from"] == 8 and rec["changes"][0]["to"] == 3

    def test_it_carries_the_hardware_it_was_given(self):
        rec = recommend({}, vram_gb=12.0, ram_gb=64.0, gpu_name="RTX 4070 Ti")
        assert rec["gpu_name"] == "RTX 4070 Ti" and rec["ram_gb"] == 64.0

    def test_empty_settings_do_not_raise(self):
        assert recommend(None)["tier"] == "tight"


class TestApply:
    def test_only_owned_keys_are_written(self):
        """An 'apply preset' button must not be an arbitrary settings writer."""
        saved = {}
        store = {"research_max_tokens": 1, "admin_password": "secret"}
        result = apply_patch(
            {"research_max_tokens": 16384, "admin_password": "pwned",
             "search_provider": "duckduckgo"},
            load=lambda: dict(store), save=lambda s: saved.update(s))
        assert saved["research_max_tokens"] == 16384
        assert saved["admin_password"] == "secret"
        assert result["ignored"] == ["admin_password"]

    def test_nothing_is_saved_when_nothing_is_owned(self):
        calls = []
        result = apply_patch({"whatever": 1}, load=lambda: {},
                             save=lambda s: calls.append(s))
        assert calls == [] and result["written"] == {}
