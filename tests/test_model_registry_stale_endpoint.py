"""A registry row is not proof that its llama.cpp endpoint is still resident."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_vendored_link_rejects_offline_registry_llamacpp_endpoint():
    from src.hoard_link.config import LinkConfig
    from src.hoard_link.link import Link

    seen: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path))
        if request.url.host == "faustus.test" and request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.host == "faustus.test" and request.url.path == "/api/models":
            return httpx.Response(200, json={"items": [{
                "url": "http://127.0.0.1:8081/v1/chat/completions",
                "models": ["qwen3.8-27b-q8-llamacpp"],
                "endpoint_id": "stale-local-endpoint",
                "model_type": "llm",
                "backend": "llamacpp",
                "category": "local",
            }]})
        if request.url.host == "127.0.0.1" and request.url.port == 8081 and request.url.path == "/props":
            return httpx.Response(503, json={"error": "server stopped"})
        # All remaining probe requests are handled locally by MockTransport;
        # this test never contacts a running model or application.
        return httpx.Response(503, json={"error": "not configured in this test"})

    config = LinkConfig(
        faustus_urls=("http://faustus.test",),
        faustus_token="test-token",
        use_routes=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
        async with Link(config, client=client) as link:
            result = await link.resolve("llm")

    assert Link.__module__ == "src.hoard_link.link"
    assert result.state == "unavailable"
    assert result.details["reasons"]
    assert any("offline, not ready" in reason for reason in result.details["reasons"])
    assert ("127.0.0.1", "/props") in seen
    assert all(host in {"faustus.test", "127.0.0.1"} for host, _ in seen)
