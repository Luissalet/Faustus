"""Real extraction of fixture pages/captures, plus bounded fan-out and HTTP wiring."""
import asyncio
import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.reach import extract as ex
from src.reach.batch import BatchReadRequest, read_many
from src.reach.base import ReachResult


def request(**kw):
    return ex.ExtractRequest(fields={"name": {"selectors": [".old", "h2"]},
                                    "link": {"selectors": ["a"], "attribute": "href"}}, **kw)


def test_rows_explicit_fallback_missing_and_provenance():
    out = asyncio.run(ex.extract(request(html='<article><h2>A</h2><a href="/a">link</a></article><article><h2>B</h2></article>', row_selectors=[".oldrow", "article"])))
    assert out["items"] == [{"name": "A", "link": "/a"}, {"name": "B", "link": None}]
    assert out["missing"] == [{"row": 1, "field": "link"}]
    assert out["selectors_used"][0]["name"]["0"] == "h2"
    assert len(out["pages"][0]["sha256"]) == 64
    assert out["untrusted_content"]


@pytest.mark.parametrize("markup", ["<p>Access denied</p>", "<p>Inicia sesión para continuar</p>"])
def test_restricted_page_is_not_a_success(markup):
    with pytest.raises(ValueError, match="restricted"):
        asyncio.run(ex.extract(request(html=markup)))


def test_pagination_uses_fetched_url_and_reports_limit(monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        html = '<article><h2>A</h2><a href="item">item</a></article><a class="next" href="/two">next</a>'
        return httpx.Response(200, text=html, headers={"content-type": "text/html"}, request=httpx.Request("GET", "https://example.org/redirected/"))
    monkeypatch.setattr(ex, "fetch_page", fetch)
    out = asyncio.run(ex.extract(request(url="https://example.org/one", row_selectors=["article"], next_selector=".next")))
    assert calls == ["https://example.org/one"]
    assert out["items"][0]["link"] == "https://example.org/redirected/item"
    assert out["truncated"] and out["reason"] == "max_pages"


def test_loop_stops_and_every_hop_uses_transport(monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        return httpx.Response(200, text='<h2>A</h2><a class="next" href="/one">next</a>', headers={"content-type": "text/html"}, request=httpx.Request("GET", url))
    monkeypatch.setattr(ex, "fetch_page", fetch)
    out = asyncio.run(ex.extract(request(url="https://example.org/one", next_selector=".next", max_pages=4)))
    assert len(calls) == 1
    assert out["reason"] == "pagination_loop"


def test_family_redirect_keeps_relative_links_at_final_origin(monkeypatch):
    monkeypatch.setattr("src.family_services.fetch", lambda *a, **kw: {
        "ok": True, "status": 200, "body": b'<h2>A</h2><a href="item">next</a>',
        "final_url": "https://example.org/new/", "headers": {"content-type": "text/html"}})
    out = asyncio.run(ex.extract(request(url="https://example.org/old")))
    assert out["items"][0]["link"] == "https://example.org/new/item"
    assert out["pages"][0]["url"] == "https://example.org/new/"


def test_scalar_json_pointer_keys_and_item_cap():
    req = ex.ExtractRequest(json_data={"rows": [{"a/b": {"~key": 0}}, {"a/b": {"~key": False}}]},
                            rows_pointer="/rows", fields={"value": {"pointer": "/a~1b/~0key"}}, max_items=1)
    out = asyncio.run(ex.extract(req))
    assert out["items"] == [{"value": 0}]
    assert out["truncated"]


def har_entry(url="https://example.org/api/items?token=SECRET", status=200, mime="application/json"):
    body = base64.b64encode(json.dumps({"items": [{"title": "A", "secret": "excluded"}]}).encode()).decode()
    return {"request": {"url": url, "headers": [{"name": "Cookie", "value": "SECRET"}]},
            "response": {"status": status, "content": {"mimeType": mime, "text": body, "encoding": "base64"}}}


def test_har_only_selected_fields_no_request_headers_or_signed_url():
    req = ex.ExtractRequest(har={"log": {"entries": [har_entry(), har_entry(status=403)]}},
                            endpoint_contains="/api/items", rows_pointer="/items", fields={"title": {"pointer": "/title"}})
    out = asyncio.run(ex.extract(req))
    assert out["items"] == [{"title": "A"}]
    assert "SECRET" not in json.dumps(out) and "excluded" not in json.dumps(out)
    assert out["pages"][0]["url"] == "https://example.org/api/items"


def test_har_ambiguity_refuses_to_pick_random_response():
    req = ex.ExtractRequest(har={"log": {"entries": [har_entry(), har_entry()]}},
                            endpoint_contains="/api/items", fields={"title": {"pointer": "/title"}})
    with pytest.raises(ValueError, match="found 2"):
        asyncio.run(ex.extract(req))


def test_batch_dedupe_failure_isolation_order_concurrency(monkeypatch):
    active, peak = 0, 0
    async def read(url, **kw):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            if url == "bad":
                raise RuntimeError("failure")
            return ReachResult(channel="web", url=url, text=url)
        finally:
            active -= 1
    monkeypatch.setattr("src.reach.router.read", read)
    out = asyncio.run(read_many(BatchReadRequest(urls=["one", "bad", "three", "one"], concurrency=2)))
    assert [r["requested_url"] for r in out["results"]] == ["one", "bad", "three"]
    assert out["succeeded"] == 2 and out["failed"] == 1 and peak == 2


def test_default_web_search_reuses_existing_provider(monkeypatch):
    monkeypatch.setattr("services.search.comprehensive_web_search", lambda *a, **k: ("", [{"url": "https://example.org", "title": "Result"}]))
    from src.reach.router import search
    out = asyncio.run(search("topic"))
    assert out["web"][0].title == "Result"
    assert out["web"][0].backend == "faustus_web_fetch"


def test_http_and_agent_surface_validate_and_execute(monkeypatch):
    from routes.reach_routes import setup_reach_routes
    from src.agent_tools.reach_tools import ReachExtractTool
    monkeypatch.setattr("core.middleware.auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_reach_routes())
    body = {"html": "<h2>Actual row</h2>", "fields": {"name": {"selectors": ["h2"]}}}
    http = TestClient(app).post("/api/reach/extract", json=body)
    assert http.status_code == 200 and http.json()["items"] == [{"name": "Actual row"}]
    tool = asyncio.run(ReachExtractTool().execute(json.dumps(body), {}))
    assert json.loads(tool["output"])["items"] == http.json()["items"]
    invalid = TestClient(app).post("/api/reach/extract", json={**body, "max_pages": 0})
    assert invalid.status_code == 422


@pytest.mark.parametrize("url", ["", "   ", "file:///tmp/data"])
def test_invalid_url_refused_before_transport(url):
    with pytest.raises(ValueError):
        request(url=url)


def test_large_tool_output_keeps_complete_rows_and_truthful_counts():
    from src.agent_tools.reach_tools import _structured_output
    from src.constants import MAX_OUTPUT_CHARS
    rows = [{"value": "x" * 4000} for _ in range(100)]
    out = _structured_output({"items": rows, "item_count": 100,
        "missing": [{"row": i, "field": "absent"} for i in range(1000)],
        "selectors_used": [{"value": {str(i): "h2" for i in range(1000)}}]})
    assert out["exit_code"] == 0 and len(out["output"]) <= MAX_OUTPUT_CHARS
    data = json.loads(out["output"])
    assert data["returned_count"] == len(data["items"]) < 100
    assert all(row == rows[0] for row in data["items"])
    assert data["item_count"] == 100 and data["missing_count"] == 1000
    assert data["metadata_truncated"] and data["output_truncated"]
