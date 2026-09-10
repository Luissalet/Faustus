"""Unit tests for src/privacy_policy.py (SEC-04/MOD-05, QA-29).

Covers the module in isolation: the local/non-local judgement, the
global-setting + project-override resolution, and `assert_outbound`'s
blocking behaviour per profile. Integration with the auxiliaries that call
`assert_outbound` (embedding lane, ChromaDB, compaction summarizer) lives in
tests/test_privacy_policy_auxiliaries.py.
"""
import pytest

from src import privacy_policy as pp


class TestIsLocalDestination:
    def test_recognizes_loopback_and_localhost(self):
        assert pp.is_local_destination("localhost:8100")
        assert pp.is_local_destination("http://127.0.0.1:11434/v1")
        assert pp.is_local_destination("http://[::1]:8000/rerank")

    def test_recognizes_private_lan_addresses(self):
        assert pp.is_local_destination("192.168.1.20:8100")
        assert pp.is_local_destination("http://10.0.0.5:8080/v1/rerank")

    def test_recognizes_dot_local_hostnames(self):
        assert pp.is_local_destination("http://myserver.local:11434/v1")

    def test_empty_destination_is_local_by_convention(self):
        # Nothing configured means nothing will be sent anywhere.
        assert pp.is_local_destination("")
        assert pp.is_local_destination(None)

    def test_rejects_public_hostnames(self):
        assert not pp.is_local_destination("https://api.openai.com/v1/embeddings")
        assert not pp.is_local_destination("reranker.example.com:8080")

    def test_rejects_public_ip_literals(self):
        assert not pp.is_local_destination("http://8.8.8.8:443/v1")


class TestGetPrivacyProfile:
    def test_defaults_to_local_preferred_with_no_setting(self, monkeypatch):
        monkeypatch.setattr(
            "src.settings.get_user_setting",
            lambda key, owner="", default=None: default,
        )
        assert pp.get_privacy_profile() == pp.PROFILE_LOCAL_PREFERRED == pp.DEFAULT_PROFILE

    def test_reads_the_global_setting(self, monkeypatch):
        monkeypatch.setattr(
            "src.settings.get_user_setting",
            lambda key, owner="", default=None: (
                pp.PROFILE_LOCAL_ONLY if key == pp.SETTING_KEY else default
            ),
        )
        assert pp.get_privacy_profile() == pp.PROFILE_LOCAL_ONLY

    def test_project_override_beats_the_global_setting(self, monkeypatch):
        monkeypatch.setattr(
            "src.settings.get_user_setting",
            lambda key, owner="", default=None: (
                pp.PROFILE_CLOUD_ALLOWED if key == pp.SETTING_KEY else default
            ),
        )
        project = {"id": "p1", pp.PROJECT_OVERRIDE_KEY: pp.PROFILE_LOCAL_ONLY}
        assert pp.get_privacy_profile(project=project) == pp.PROFILE_LOCAL_ONLY

    def test_project_without_the_field_falls_back_to_global(self, monkeypatch):
        monkeypatch.setattr(
            "src.settings.get_user_setting",
            lambda key, owner="", default=None: (
                pp.PROFILE_CLOUD_ALLOWED if key == pp.SETTING_KEY else default
            ),
        )
        project = {"id": "p1", "name": "No override here"}
        assert pp.get_privacy_profile(project=project) == pp.PROFILE_CLOUD_ALLOWED

    def test_unknown_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(
            "src.settings.get_user_setting",
            lambda key, owner="", default=None: "definitely-not-a-real-profile",
        )
        assert pp.get_privacy_profile() == pp.DEFAULT_PROFILE

    def test_settings_module_unavailable_falls_back_to_default(self, monkeypatch):
        def _boom(*a, **k):
            raise ImportError("no settings module")
        monkeypatch.setattr("src.settings.get_user_setting", _boom)
        assert pp.get_privacy_profile() == pp.DEFAULT_PROFILE


class TestAssertOutbound:
    def test_blocks_a_remote_destination_under_local_only(self):
        with pytest.raises(pp.PrivacyPolicyError) as exc_info:
            pp.assert_outbound(
                "reranker", "https://reranker.example.com/v1/rerank",
                profile=pp.PROFILE_LOCAL_ONLY,
            )
        err = exc_info.value
        assert err.component == "reranker"
        assert err.profile == pp.PROFILE_LOCAL_ONLY
        assert err.error_info.code == "privacy.blocked_outbound"
        assert err.error_info.retryable is False
        assert err.error_info.next_action == "change_privacy_profile_or_component"

    def test_allows_a_local_destination_under_local_only(self):
        # Must not raise.
        pp.assert_outbound(
            "reranker", "http://127.0.0.1:8080/v1/rerank", profile=pp.PROFILE_LOCAL_ONLY,
        )

    @pytest.mark.parametrize("profile", [pp.PROFILE_LOCAL_PREFERRED, pp.PROFILE_CLOUD_ALLOWED])
    def test_never_blocks_outside_local_only(self, profile):
        pp.assert_outbound("reranker", "https://reranker.example.com/v1/rerank", profile=profile)

    def test_reads_the_effective_profile_when_none_is_passed_explicitly(self, monkeypatch):
        monkeypatch.setattr(pp, "get_privacy_profile", lambda project=None, owner=None: pp.PROFILE_LOCAL_ONLY)
        with pytest.raises(pp.PrivacyPolicyError):
            pp.assert_outbound("embeddings", "https://embeddings.example.com/v1")


def test_local_only_policy_covers_auxiliaries_marker_exists():
    # What the (now closed) QA-29 test asserts on.
    assert pp.local_only_policy_covers_auxiliaries() is True
