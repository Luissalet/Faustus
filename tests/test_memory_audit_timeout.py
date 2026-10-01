from pathlib import Path


def test_memory_audit_uses_its_own_llm_timeout():
    source = Path("app.py").read_text(encoding="utf-8")
    start = source.index("_TIMEOUT_EXEMPT_PREFIXES =")
    end = source.index("\n)\n", start)
    timeout_exemptions = source[start:end]

    assert '"/api/memory/audit"' in timeout_exemptions


def test_long_running_routes_are_exempt_from_the_45s_request_timeout():
    """Each of these runs a model or a renderer inside the request; at 45 s the
    client got a 504 while the work carried on."""
    source = Path("app.py").read_text(encoding="utf-8")
    start = source.index("_TIMEOUT_EXEMPT_PREFIXES =")
    exemptions = source[start:source.index("\n)\n", start)]
    for prefix in ('"/api/bug-hunt"', '"/api/blender/run"', '"/api/workflows/runs"'):
        assert prefix in exemptions, prefix
