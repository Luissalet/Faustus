"""`page_prune`: the builtin tool over src/research_prune.py -- schema, the
registry entries the catalog tests rely on, and the executor's paths."""
import asyncio
import json
from pathlib import Path

import pytest

from src.agent_tools import TOOL_TAGS
from src.agent_tools.prune_tools import PagePruneTool
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

FIXTURES = Path(__file__).parent / "fixtures" / "research_pages"


def _schema():
    return next(t["function"] for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "page_prune")


def _run(args):
    return asyncio.run(PagePruneTool().execute(json.dumps(args), {}))


def test_schema_is_well_formed_and_requires_a_query():
    fn = _schema()
    assert fn["parameters"]["required"] == ["query"]
    props = fn["parameters"]["properties"]
    for key in ("query", "url", "html", "text", "max_chars", "threshold", "include_blocks", "title"):
        assert key in props
    assert props["max_chars"]["type"] == "integer" and props["threshold"]["type"] == "number"


def test_tool_is_registered_everywhere_the_catalogs_look():
    from src.agent_tools import TOOL_HANDLERS
    from src.tool_capabilities import ResultIntegrity, ToolEffect, capabilities_for_tool
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES

    assert "page_prune" in TOOL_TAGS
    assert "page_prune" in TOOL_HANDLERS
    assert BUILTIN_TOOL_DESCRIPTIONS["page_prune"]
    assert len(EXAMPLES["page_prune"]) >= 3
    caps = capabilities_for_tool("page_prune")
    assert caps.known and ToolEffect.BROKERED_NETWORK_READ in caps.effects
    assert caps.result_integrity == ResultIntegrity.EXTERNAL_UNTRUSTED


def test_prunes_html_and_reports_sizes_and_scores():
    html = (FIXTURES / "whiplash_es.html").read_text(encoding="utf-8")
    out = _run({"query": "signos de alarma derivación", "html": html, "max_chars": 800})
    assert out["exit_code"] == 0
    assert out["stats"]["pruned_chars"] < out["stats"]["original_chars"]
    assert out["stats"]["top_bm25"] > 0 and out["stats"]["blocks_kept"] >= 1
    assert "Se debe derivar ante sospecha" in out["pruned_text"]
    assert "Utilizamos cookies" not in out["pruned_text"]
    assert "[KEPT]" in out["output"] and "[drop]" in out["output"]
    assert out["untrusted_content"] is True


def test_include_blocks_false_omits_the_score_table():
    out = _run({"query": "token bucket", "html": (FIXTURES / "rate_limit_en.html").read_text(encoding="utf-8"),
                "include_blocks": False})
    assert "Block scores" not in out["output"] and out["exit_code"] == 0


def test_prunes_plain_text_input():
    text = "# Title\n\n" + "Retries use exponential backoff with jitter to avoid a thundering herd. " * 6 + "\n\nAll rights reserved."
    out = _run({"query": "backoff jitter", "text": text})
    assert out["exit_code"] == 0 and "backoff" in out["pruned_text"]
    assert "All rights reserved" not in out["pruned_text"]


def test_fetches_a_url_with_the_markup(monkeypatch):
    html = (FIXTURES / "rate_limit_en.html").read_text(encoding="utf-8")
    seen = {}

    def fake_fetch(url, timeout=5, **kw):
        seen.update(url=url, kw=kw)
        return {"success": True, "content": "flat text", "raw_html": html, "title": "Page", "error": ""}

    monkeypatch.setattr("src.search.content.fetch_webpage_content", fake_fetch)
    out = _run({"query": "token bucket burst", "url": "docs.example.test/rate"})
    assert seen["url"] == "https://docs.example.test/rate" and seen["kw"] == {"keep_html": True}
    assert out["exit_code"] == 0 and "token bucket" in out["pruned_text"].lower()


@pytest.mark.parametrize("args,needle", [
    ({"url": "https://x.test"}, "provide a query"),
    ({"query": "q"}, "provide one of url, html or text"),
    ({"query": "q", "url": "ftp://x.test/a"}, "unsupported URL scheme"),
])
def test_bad_arguments_are_reported_not_raised(args, needle):
    out = _run(args)
    assert out["exit_code"] == 1 and needle in out["error"]


def test_fetch_failure_is_reported(monkeypatch):
    monkeypatch.setattr("src.search.content.fetch_webpage_content",
                        lambda url, timeout=5, **kw: {"success": False, "content": "", "error": "HTTP 404: nope"})
    out = _run({"query": "q", "url": "https://x.test/missing"})
    assert out["exit_code"] == 1 and "HTTP 404" in out["error"]


def test_bare_non_json_content_is_an_error_not_a_crash():
    out = asyncio.run(PagePruneTool().execute("just some words", {}))
    assert out["exit_code"] == 1


def test_oversized_input_is_refused():
    out = _run({"query": "q", "text": "x " * 1_100_000})
    assert out["exit_code"] == 1 and "larger than" in out["error"]
