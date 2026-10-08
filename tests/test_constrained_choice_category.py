"""OBJ-27 phase 1: Deep Research's report-category decision on the constrained path.

The migrated point is `DeepResearcher._classify_category`. These tests run the
SAME 24 questions through the previous free-text classifier
(`_classify_category_free_text`) and the new one against the benchmark stub, and
check (a) parity where the previous path was right, (b) that the new answer is
never outside the category set, (c) that the constraint reaches the wire only
on the backends that take it, and (d) that the switch restores the old call.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import asynccontextmanager

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import constrained_choice as cc  # noqa: E402
from src.deep_research import CATEGORY_PROMPTS, DeepResearcher  # noqa: E402


def _bench():
    name = "constrained_choice_bench"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", "constrained_choice_bench.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bench = _bench()
NONE_LABEL = DeepResearcher.CATEGORY_NONE_LABEL
REFERENCE = [(q, None if c == NONE_LABEL else c) for q, c in bench.CASES]
assert len(REFERENCE) >= 20


@pytest.fixture(autouse=True)
def _fresh():
    cc._reset_stats()
    yield
    cc._reset_stats()


@asynccontextmanager
async def serve(mode, monkeypatch, **kw):
    from src import llm_core
    kw.setdefault("simulate", False)
    stub = bench.StubModel(mode, **kw)
    server, port = bench.start_stub(stub)
    client = httpx.AsyncClient()
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)

    @asynccontextmanager
    async def no_gate(*a, **k):
        yield

    monkeypatch.setattr(llm_core, "_local_model_slot", no_gate)
    try:
        yield stub, bench.endpoint_url(mode, port)
    finally:
        await client.aclose()
        server.shutdown()


def researcher(url):
    r = DeepResearcher.__new__(DeepResearcher)
    r.llm_endpoint, r.llm_model, r.llm_headers = url, "stub-model", {}
    r._failures, r._think_overrides, r._read_overrides = [], None, None
    return r


async def run_both(url):
    old, new = [], []
    for q, _ in REFERENCE:
        old.append(await researcher(url)._classify_category_free_text(q))
        r = researcher(url)
        new.append(await r._classify_category(q))
    return old, new


async def test_parity_with_a_disciplined_model_on_every_backend(monkeypatch):
    """A model that already answers with the bare label: both paths agree on all 24."""
    for mode in ("llamacpp", "ollama", "plain"):
        async with serve(mode, monkeypatch, styles=("clean",)) as (stub, url):
            old, new = await run_both(url)
        expected = [c for _, c in REFERENCE]
        assert old == expected, mode
        assert new == expected, mode


@pytest.mark.parametrize("mode", ["llamacpp", "ollama"])
async def test_constrained_backends_are_legal_where_the_previous_path_is_not(monkeypatch, mode):
    async with serve(mode, monkeypatch) as (stub, url):   # default scripted styles: wordy / invented replies
        old, new = await run_both(url)
        grammar_or_format = [e for e in stub.log if e["constraint"] in ("grammar", "format")]
    expected = [c for _, c in REFERENCE]
    assert new == expected                                  # every one of the 24, no misses
    assert all(c is None or c in CATEGORY_PROMPTS for c in new)
    assert len(grammar_or_format) == len(REFERENCE)         # one constrained request per question, no repair
    # the previous path lost the categories the scripted model invented words for
    lost = [i for i, (o, e) in enumerate(zip(old, expected)) if o != e]
    assert lost, "the scripted scenario should make the old parser miss some"
    assert all(bench.style_for(i) == "invented" for i in lost)


async def test_plain_backend_repairs_instead_of_losing_the_category(monkeypatch):
    async with serve("plain", monkeypatch) as (stub, url):
        old, new = await run_both(url)
        repairs = [e for e in stub.log if e["is_repair"]]
        grammars = [e for e in stub.log if e["constraint"] != "none"]
    expected = [c for _, c in REFERENCE]
    assert new == expected
    assert len(repairs) == sum(1 for i in range(len(REFERENCE)) if bench.style_for(i) == "invented")
    assert grammars == []                                    # nothing a strict server could reject


async def test_the_decision_is_recorded_for_the_run(monkeypatch):
    async with serve("llamacpp", monkeypatch) as (stub, url):
        r = researcher(url)
        category = await r._classify_category(REFERENCE[6][0])
    assert category == "comparison"
    d = r.category_decision
    assert d["path"] == "llamacpp_grammar" and d["honoured"] is True and d["choice"] == "comparison"
    assert "attempts" not in d


async def test_general_means_no_category(monkeypatch):
    async with serve("llamacpp", monkeypatch) as (stub, url):
        r = researcher(url)
        assert await r._classify_category("History of the Roman Empire's decline") is None
        assert r.category_decision["choice"] == NONE_LABEL   # a legal answer, mapped to "no category"


async def test_switch_off_restores_the_previous_call_exactly(monkeypatch):
    import src.settings as settings
    real = settings.get_setting
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: False if key == "constrained_choice_enabled" else real(key, default))
    async with serve("llamacpp", monkeypatch, styles=("clean",)) as (stub, url):
        got = await researcher(url)._classify_category(REFERENCE[0][0])
        sent = list(stub.log)
    assert got == "product"
    assert len(sent) == 1 and sent[0]["constraint"] == "none" and sent[0]["max_tokens"] == 20


async def test_a_dead_endpoint_degrades_to_none_and_notes_the_failure(monkeypatch):
    from src import llm_core

    async def refuse(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(llm_core, "llm_call_async", refuse)
    r = researcher("http://127.0.0.1:1/v1/chat/completions")
    monkeypatch.setattr(cc, "resolve_backend", lambda url, forced=None: "other")
    got = await r._classify_category("Best standing desk under 400 dollars")
    assert got is None
    assert any(f.startswith("category:") for f in r._failures)


async def test_the_previous_keyword_scan_could_pick_the_wrong_category(monkeypatch):
    """The old parser took the first category (in dict order) named anywhere in the
    reply, so a sentence that REJECTS a category selected it. The new reader refuses
    an ambiguous reply and asks once more instead of guessing."""
    wordy = "This is not a product question, it is a howto: the user wants steps."
    r = researcher("http://stub")

    async def fake_llm(*a, **k):
        return wordy

    r._llm = fake_llm
    assert await r._classify_category_free_text("How do I rotate API keys?") == "product"   # the wrong one
    assert cc.match_option(wordy, list(CATEGORY_PROMPTS) + [NONE_LABEL]) is None            # now refused


def test_none_label_is_not_a_report_format():
    assert NONE_LABEL not in CATEGORY_PROMPTS
