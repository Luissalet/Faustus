"""`POST /api/session/{id}/condense` is one LLM pass on a hand-picked range,
run on the session's own (possibly cold, local) model. app.py's 45s hard
timeout killed it live before the model had even loaded; the route is
exempt by SUFFIX because the session id sits in the middle of the path."""
from pathlib import Path


def _middleware_source():
    return (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def test_condense_is_exempt_from_the_hard_request_timeout():
    source = _middleware_source()
    assert '_TIMEOUT_EXEMPT_SUFFIXES = ("/condense",)' in source
    assert "path.endswith(_TIMEOUT_EXEMPT_SUFFIXES)" in source


def test_condense_gives_a_cold_model_minutes_not_seconds():
    from src import condense
    from src import context_compactor as cc
    assert condense.CONDENSE_LLM_TIMEOUT_S >= 180
    # the mid-turn compactor keeps its short default: it runs inside a live turn
    import inspect
    assert inspect.signature(cc.summarize_rows).parameters["timeout"].default == 30
