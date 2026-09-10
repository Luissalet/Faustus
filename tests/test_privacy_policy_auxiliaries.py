"""Each auxiliary that ships user content off-box consults src/privacy_policy
before opening the connection (SEC-04/MOD-05, QA-29).

Verifies the three wired call sites directly:
  - src/embedding_lanes.py's custom/HTTP embedding lane
  - src/chroma_client.py's ChromaDB connection
  - src/context_compactor.py's remote compaction summarizer

Each test proves TWO things: the policy is actually consulted (not just that
a remote call happens to fail for some other reason), and a block degrades
the same way any other auxiliary failure already does (no crash, no silent
data loss) rather than being swallowed invisibly.
"""
import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from src import privacy_policy as pp

# ── embedding_lanes ──────────────────────────────────────────────────────

import src.embedding_lanes as lanes
from tests.helpers.embedding_lanes import FakeChroma, FakeEmbedder, patch_chroma


class TestEmbeddingLanePrivacyGate:
    def test_custom_lane_consults_the_policy_before_building_a_client(self, monkeypatch):
        calls = []

        def fake_assert_outbound(component, destination, **kwargs):
            calls.append((component, destination))
            raise pp.PrivacyPolicyError(
                component, destination, pp.PROFILE_LOCAL_ONLY,
                pp.ErrorInfo(code="privacy.blocked_outbound", message="blocked"),
            )

        monkeypatch.setattr(pp, "assert_outbound", fake_assert_outbound)
        monkeypatch.setattr(
            lanes, "_load_custom_endpoint",
            lambda: {"url": "https://embeddings.example.com/v1", "model": "m", "api_key": ""},
        )

        with pytest.raises(pp.PrivacyPolicyError):
            lanes._build_custom_client()

        assert calls == [("embeddings", "https://embeddings.example.com/v1")]

    def test_no_configured_endpoint_never_calls_the_policy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(pp, "assert_outbound", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(lanes, "_load_custom_endpoint", lambda: {})

        assert lanes._build_custom_client() is None
        assert calls == []

    def test_build_embedding_lanes_degrades_to_fastembed_when_custom_lane_is_blocked(self, monkeypatch):
        fake = FakeChroma()
        patch_chroma(monkeypatch, fake)

        def _blocked():
            raise pp.PrivacyPolicyError(
                "embeddings", "https://embeddings.example.com/v1", pp.PROFILE_LOCAL_ONLY,
                pp.ErrorInfo(code="privacy.blocked_outbound", message="blocked"),
            )

        monkeypatch.setattr(lanes, "_build_custom_client", _blocked)
        monkeypatch.setattr(
            lanes, "_build_fastembed_client",
            lambda: FakeEmbedder(384, "sentence-transformers/all-MiniLM-L6-v2", "local://fastembed"),
        )

        built = lanes.build_embedding_lanes("odysseus_memories")

        assert [lane.name for lane in built] == [lanes.LANE_FASTEMBED]


# ── chroma_client ────────────────────────────────────────────────────────

import src.chroma_client as chroma_client


class TestChromaClientPrivacyGate:
    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        chroma_client.reset_client()
        yield
        chroma_client.reset_client()

    def test_remote_host_is_blocked_under_local_only(self, monkeypatch):
        monkeypatch.setenv("CHROMADB_HOST", "chroma.example.com")
        monkeypatch.setenv("CHROMADB_PORT", "8100")
        monkeypatch.setattr(pp, "get_privacy_profile", lambda project=None, owner=None: pp.PROFILE_LOCAL_ONLY)

        with pytest.raises(pp.PrivacyPolicyError) as exc_info:
            chroma_client.get_chroma_client()
        assert exc_info.value.error_info.code == "privacy.blocked_outbound"

    def test_local_host_passes_the_privacy_gate(self, monkeypatch):
        # localhost must never trip the privacy gate even under local_only —
        # whatever happens after (this sandbox has no ChromaDB listening) is
        # a plain "not reachable" RuntimeError, not a privacy block.
        monkeypatch.setenv("CHROMADB_HOST", "localhost")
        monkeypatch.setenv("CHROMADB_PORT", "8100")
        monkeypatch.setattr(pp, "get_privacy_profile", lambda project=None, owner=None: pp.PROFILE_LOCAL_ONLY)

        with pytest.raises(RuntimeError) as exc_info:
            chroma_client.get_chroma_client()
        assert not isinstance(exc_info.value, pp.PrivacyPolicyError)

    def test_remote_host_is_not_blocked_under_local_preferred(self, monkeypatch):
        monkeypatch.setenv("CHROMADB_HOST", "chroma.example.com")
        monkeypatch.setenv("CHROMADB_PORT", "8100")
        monkeypatch.setattr(pp, "get_privacy_profile", lambda project=None, owner=None: pp.PROFILE_LOCAL_PREFERRED)

        # Not blocked by privacy — falls through to the ordinary
        # unreachable-host error instead.
        with pytest.raises(RuntimeError) as exc_info:
            chroma_client.get_chroma_client()
        assert not isinstance(exc_info.value, pp.PrivacyPolicyError)


# ── context_compactor ────────────────────────────────────────────────────

for _mod in [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database', 'core.models', 'core.database',
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import src.context_compactor as cc


def _big_history():
    # Large system prompt so a tiny context_length forces the real
    # compaction branch, same recipe as tests/test_context_compactor.py.
    return [
        {"role": "system", "content": "You are a helpful agent. " * 200},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "reply 2"},
        {"role": "user", "content": "turn 3 — the newest turn"},
    ]


class TestCompactionSummarizerPrivacyGate:
    def _run(self, monkeypatch, *, compact_url):
        monkeypatch.setattr(cc, "get_context_length", lambda url, model: 500)
        monkeypatch.setattr(cc, "resolve_endpoint", lambda which, owner=None: (compact_url, "util-model", {}))
        monkeypatch.setattr(cc, "_update_session_history", lambda *a, **k: None)

        calls = []

        async def _fake_summary(*a, **k):
            calls.append(a)
            return "compact summary text"

        monkeypatch.setattr(cc, "llm_call_async", _fake_summary)

        result = asyncio.run(cc.maybe_compact(
            session=None,
            endpoint_url="http://local/v1/chat/completions",
            model="local-model",
            messages=_big_history(),
            headers={},
        ))
        return result, calls

    def test_remote_utility_endpoint_is_blocked_under_local_only(self, monkeypatch):
        monkeypatch.setattr(pp, "get_privacy_profile", lambda project=None, owner=None: pp.PROFILE_LOCAL_ONLY)
        (messages, _context_length, was_compacted), calls = self._run(
            monkeypatch, compact_url="https://api.example.com/v1",
        )
        assert was_compacted is False
        assert calls == []  # the LLM summarizer was never reached

    def test_local_utility_endpoint_is_not_blocked_under_local_only(self, monkeypatch):
        monkeypatch.setattr(pp, "get_privacy_profile", lambda project=None, owner=None: pp.PROFILE_LOCAL_ONLY)
        (_messages, _context_length, was_compacted), calls = self._run(
            monkeypatch, compact_url="http://127.0.0.1:11434/v1",
        )
        assert was_compacted is True
        assert len(calls) == 1

    def test_remote_utility_endpoint_is_not_blocked_under_local_preferred(self, monkeypatch):
        monkeypatch.setattr(pp, "get_privacy_profile", lambda project=None, owner=None: pp.PROFILE_LOCAL_PREFERRED)
        (_messages, _context_length, was_compacted), calls = self._run(
            monkeypatch, compact_url="https://api.example.com/v1",
        )
        assert was_compacted is True
        assert len(calls) == 1
