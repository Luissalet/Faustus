"""A token-supplied public endpoint stays public at connect time."""

import asyncio
import ipaddress

import src.llm_core as llm_core
import src.webhook_manager as webhooks


class _Response:
    is_success = True
    status_code = 200
    text = ""

    def json(self):
        return {"model": "safe-model", "choices": [{"message": {"content": "ok"}}]}


def test_direct_session_marker_pins_dns_and_never_reaches_provider(monkeypatch):
    captured = {}

    class _Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Transport:
        def __init__(self, ip):
            captured["ip"] = ip

    async def _post(client, url, headers, **kwargs):
        captured["url"] = url
        captured["headers"] = dict(headers)
        return _Response()

    monkeypatch.setattr(webhooks, "_validated_public_ips",
                        lambda url: [ipaddress.ip_address("93.184.216.34")])
    monkeypatch.setattr(webhooks, "_PinnedAsyncTransport", _Transport)
    monkeypatch.setattr(llm_core.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", _post)
    monkeypatch.setattr(
        llm_core, "_get_http_client",
        lambda: (_ for _ in ()).throw(AssertionError("shared DNS client was used")),
    )
    llm_core._response_cache.clear()

    result = asyncio.run(llm_core.llm_call_async(
        "https://api.example.test/v1/chat/completions",
        "safe-model",
        [{"role": "user", "content": "unique dns pin request"}],
        headers={
            "Authorization": "Bearer secret",
            "X-Faustus-Public-DNS-Pin": "1",
        },
        max_retries=1,
    ))

    assert result == "ok"
    assert str(captured["ip"]) == "93.184.216.34"
    assert captured["client"]["follow_redirects"] is False
    assert captured["client"]["trust_env"] is False
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert not any(key.lower() == "x-faustus-public-dns-pin"
                   for key in captured["headers"])


def test_dns_pin_fails_closed_if_name_rebinds_private(monkeypatch):
    monkeypatch.setattr(
        webhooks, "_validated_public_ips",
        lambda url: (_ for _ in ()).throw(
            ValueError("URL must not point to private/internal addresses")
        ),
    )
    llm_core._response_cache.clear()

    try:
        asyncio.run(llm_core.llm_call_async(
            "https://rebound.example/v1/chat/completions",
            "safe-model",
            [{"role": "user", "content": "unique rebinding request"}],
            headers={"X-Faustus-Public-DNS-Pin": "1"},
            max_retries=1,
        ))
    except llm_core.HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("a private rebinding target was accepted")
