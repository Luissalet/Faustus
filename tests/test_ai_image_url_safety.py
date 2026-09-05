from src import ai_interaction


class _GenerationResponse:
    status_code = 200
    text = ""

    def __init__(self, image_url):
        self._image_url = image_url

    def json(self):
        return {"data": [{"url": self._image_url}]}


class _DownloadResponse:
    status_code = 503
    content = b""


def _patch_generation(monkeypatch, image_url):
    async def _post(self, url, json, headers):
        return _GenerationResponse(image_url)

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        post = _post

    import httpx
    import src.settings as settings

    monkeypatch.setattr(settings, "load_settings", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(
        ai_interaction,
        "_resolve_model",
        lambda model_spec, owner=None: (
            "https://api.openai.example/v1/chat/completions",
            "dall-e-3",
            {"Authorization": "Bearer test"},
        ),
    )


async def test_generate_image_downloads_provider_url_through_the_broker(monkeypatch):
    """The download must go through outbound_fetch.fetch, not a bare httpx.get.

    A plain httpx.get re-resolves the hostname the guard just approved, which is
    the DNS-rebinding window B-019 describes; the broker resolves once and pins.
    """
    import httpx
    from src import outbound_fetch

    provider_url = "https://images.example.com/generated.png?sig=abc"
    events = []
    _patch_generation(monkeypatch, provider_url)

    def _fetch(url, **kwargs):
        events.append(("fetch", url, kwargs["profile"], kwargs["allow_local"]))
        raise httpx.ConnectError("no network in tests")

    def _get(url, *args, **kwargs):
        raise AssertionError("image download must not use an unpinned httpx.get")

    monkeypatch.setattr(outbound_fetch, "fetch", _fetch)
    monkeypatch.setattr(httpx, "get", _get)

    result = await ai_interaction.do_generate_image("draw a chair\ndall-e-3")

    # Download failed, so the tool falls back to handing back the external URL.
    assert result["image_url"] == provider_url
    assert events == [
        ("fetch", provider_url, outbound_fetch.PROVIDER_RESULT, False),
    ]


async def test_generate_image_rejects_unsafe_provider_url_without_download(monkeypatch):
    unsafe_url = "http://169.254.169.254/latest/meta-data"
    _patch_generation(monkeypatch, unsafe_url)

    result = await ai_interaction.do_generate_image("draw a chair\ndall-e-3")

    assert "unsafe image URL" in result["error"]
    assert "link-local" in result["error"]
