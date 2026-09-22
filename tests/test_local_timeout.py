"""A local model that reasons does not fit in a timeout written for an API.

Found by using the app: a three-sentence explanation came back as a 502 after
30.2 seconds. The model was fine -- it was still working. `LLMConfig`'s
30-second default is a sensible ceiling for a hosted API and a guaranteed
cut-off for a quantised 27B that reasons first.

Measured on this machine, reasoning on:

    three sentences  -> 23.0 s   (243 tokens)
    a longer answer  -> 74.9 s   (800 tokens, hit the ceiling)

So every local pass that was not trivial was cutting itself off. The fix
raises the DEFAULT for local endpoints only; a caller that names a timeout
still gets exactly what it asked for, which is what keeps the fast internal
passes fast.
"""
import pytest

import src.llm_core as llm_core
from src.llm_core import LLMConfig


LLAMACPP = "http://127.0.0.1:8081/v1"
OLLAMA = "http://127.0.0.1:11434"
HOSTED = "https://api.example-openai.test/v1"


@pytest.fixture
def backends(monkeypatch):
    import src.model_backend as model_backend
    table = {LLAMACPP: "llamacpp", HOSTED: "remote"}

    def fake(base_url, *, endpoint_kind=None, probe=True):
        return {"backend": table.get(str(base_url), "unknown"), "label": "x"}

    monkeypatch.setattr(model_backend, "serving_backend", fake)


# ---------------------------------------------------------------------------
# Which endpoints are ours
# ---------------------------------------------------------------------------

def test_a_managed_llamacpp_is_local(backends):
    assert llm_core.is_local_backend(LLAMACPP) is True


def test_ollama_is_local(backends):
    assert llm_core.is_local_backend(OLLAMA) is True


def test_a_hosted_api_is_not_local(backends):
    assert llm_core.is_local_backend(HOSTED) is False


def test_an_unclassifiable_endpoint_is_not_local(monkeypatch):
    """Unknown means treat it like an API: the short timeout is the safe
    default, and a wrong "local" guess would hang a hosted call for minutes."""
    import src.model_backend as model_backend

    def boom(*a, **k):
        raise RuntimeError("no registry")

    monkeypatch.setattr(model_backend, "serving_backend", boom)
    assert llm_core.is_local_backend("https://somewhere.test/v1") is False


# ---------------------------------------------------------------------------
# What the default becomes
# ---------------------------------------------------------------------------

def test_a_local_endpoint_gets_room_to_think(backends):
    assert llm_core.resolve_timeout(LLAMACPP, LLMConfig.DEFAULT_TIMEOUT) == \
        LLMConfig.LOCAL_DEFAULT_TIMEOUT


def test_the_local_default_clears_the_measured_worst_case(backends):
    """74.9 s was a real answer being cut off, not a hang."""
    assert LLMConfig.LOCAL_DEFAULT_TIMEOUT > 75


def test_a_hosted_endpoint_keeps_the_short_default(backends):
    assert llm_core.resolve_timeout(HOSTED, LLMConfig.DEFAULT_TIMEOUT) == \
        LLMConfig.DEFAULT_TIMEOUT


def test_a_caller_that_named_a_timeout_is_obeyed(backends):
    """This is what keeps the deliberately fast passes fast -- the residency
    ping wants 3 seconds and must still get 3 seconds."""
    assert llm_core.resolve_timeout(LLAMACPP, 3) == 3
    assert llm_core.resolve_timeout(LLAMACPP, 600) == 600
    assert llm_core.resolve_timeout(HOSTED, 5) == 5


def test_none_behaves_like_the_default(backends):
    assert llm_core.resolve_timeout(LLAMACPP, None) == LLMConfig.LOCAL_DEFAULT_TIMEOUT
    assert llm_core.resolve_timeout(HOSTED, None) == LLMConfig.DEFAULT_TIMEOUT
