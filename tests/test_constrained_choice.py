"""Tests for src/constrained_choice.py (OBJ-27 phase 1).

* the grammar / enum builders (quotes, backslashes, unicode, labels that are
  prefixes of each other, empty and very large sets), checked against an
  INDEPENDENT reader of the grammar (the benchmark stub's);
* the lenient answer reader (`match_option`) - ambiguity is None, never a guess;
* backend routing: llama.cpp -> `grammar`, Ollama -> `format` with an enum,
  everything else -> free text; a grammar is never sent to a server that was
  not identified as llama.cpp;
* `choose_one` against the benchmark's stub server over real HTTP: the
  constraint reaches the wire, the answer is always inside the set, a server
  that ignores or rejects the constraint is handled, one repair, no repair,
  timeouts, no endpoint.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from contextlib import asynccontextmanager

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import constrained_choice as cc  # noqa: E402


def _load_bench():
    name = "constrained_choice_bench"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", "constrained_choice_bench.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bench = _load_bench()


@pytest.fixture(autouse=True)
def _fresh_state():
    cc._reset_stats()
    yield
    cc._reset_stats()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def test_gbnf_is_a_literal_alternation():
    assert cc.build_gbnf(["product", "howto"]) == 'root ::= "product" | "howto"'


@pytest.mark.parametrize("labels", [
    ['say "hi"', "back\\slash", "tab\there", "line\nbreak"],
    ["categoría", "日本語", "naïve — dash", "emoji 😀"],
    ["a", "ab", "abc", "b"],                       # prefixes of each other
    ["x\x01y", "del\x7f", "carriage\rreturn"],      # control characters
    ["root ::= evil", '" | "', "\\\"", "end\\"],    # grammar syntax inside labels
])
def test_gbnf_round_trips_through_an_independent_reader(labels):
    grammar = cc.build_gbnf(labels)
    assert grammar.count("\n") == 0  # one line: a label cannot open a new rule
    assert bench.grammar_labels(grammar) == [l.strip() for l in labels]


def test_prefix_labels_are_all_present_and_distinct():
    assert bench.grammar_labels(cc.build_gbnf(["a", "ab"])) == ["a", "ab"]


def test_empty_set_is_rejected():
    for bad in ([], (), None):
        with pytest.raises(ValueError):
            cc.normalize_options(bad)
    with pytest.raises(ValueError):
        cc.build_gbnf([])
    with pytest.raises(ValueError):
        cc.build_enum_schema([])


def test_a_bare_string_is_not_an_option_list():
    with pytest.raises(ValueError):
        cc.normalize_options("product")


def test_blank_and_oversized_labels_are_rejected():
    with pytest.raises(ValueError):
        cc.normalize_options(["ok", "   "])
    with pytest.raises(ValueError):
        cc.normalize_options(["x" * (cc.MAX_LABEL_CHARS + 1)])


def test_options_are_stripped_and_deduplicated_in_order():
    assert cc.normalize_options([" b ", "a", "b", "a", 3]) == ["b", "a", "3"]


def test_a_very_large_set_builds_and_stays_one_line():
    labels = [f"option-{i:04d}-" + "é" * 20 for i in range(cc.MAX_OPTIONS)]
    grammar = cc.build_gbnf(labels)
    assert bench.grammar_labels(grammar) == labels
    assert "\n" not in grammar
    schema = cc.build_enum_schema(labels)
    assert len(schema["enum"]) == cc.MAX_OPTIONS
    json.dumps(schema)


def test_more_than_the_maximum_is_rejected():
    with pytest.raises(ValueError):
        cc.normalize_options([f"o{i}" for i in range(cc.MAX_OPTIONS + 1)])


def test_enum_schema_shape():
    schema = cc.build_enum_schema(["a", 'q"uote', "ü"])
    assert schema == {"type": "string", "enum": ["a", 'q"uote', "ü"]}
    assert json.loads(json.dumps(schema)) == schema


def test_ollama_answers_are_read_as_a_json_string_or_a_wrapped_object():
    assert cc._ollama_value('"product"') == "product"
    assert cc._ollama_value(' "howto" \n') == "howto"
    assert cc._ollama_value('{"choice": "general"}') == "general"
    assert cc._ollama_value("product") is None      # not JSON: left to the lenient reader
    assert cc._ollama_value("3") is None and cc._ollama_value("") is None


def test_messages_list_the_options_unless_told_not_to():
    msgs = cc.build_messages("Pick.", ["a", "b"], system="sys")
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert "Options: a | b" in msgs[1]["content"]
    plain = cc.build_messages("Pick.", ["a", "b"], append_options=False)
    assert plain == [{"role": "user", "content": "Pick."}]


def test_repair_messages_restate_the_set_and_the_bad_reply():
    base = cc.build_messages("Pick.", ["a", "b"])
    fix = cc.build_repair_messages(base, ["a", "b"], "I think maybe c")
    assert fix[:-1] == base
    assert "not one of the allowed options" in fix[-1]["content"]
    assert "a | b" in fix[-1]["content"] and "I think maybe c" in fix[-1]["content"]


# ---------------------------------------------------------------------------
# Reading answers
# ---------------------------------------------------------------------------

OPTS = ["product", "comparison", "howto", "factcheck", "general"]


@pytest.mark.parametrize("text,expected", [
    ("product", "product"),
    ("  Product \n", "product"),
    ("PRODUCT.", "product"),
    ('"howto"', "howto"),
    ("**factcheck**", "factcheck"),
    ("```\ncomparison\n```", "comparison"),
    ("The category is product.", "product"),
    ("howto - because it asks for steps", "howto"),
    ('{"choice": "general"}', "general"),
    ('{"answer": "howto"}', "howto"),
    ('"comparison"', "comparison"),
    ("<think>maybe howto? no</think>factcheck", "factcheck"),
    ("Category: comparison", "comparison"),
])
def test_match_option_accepts(text, expected):
    assert cc.match_option(text, OPTS) == expected


@pytest.mark.parametrize("text", [
    "", "   ", None, "shopping", "overview", "I am not sure",
    "product or comparison",              # two different options: ambiguous
    "<think>still thinking about howto",   # unterminated reasoning: no answer yet
    '{"choice": 3}', "[1,2]",
])
def test_match_option_refuses_instead_of_guessing(text):
    assert cc.match_option(text, OPTS) is None


def test_match_option_with_prefix_labels():
    opts = ["a", "ab", "abc"]
    assert cc.match_option("a", opts) == "a"
    assert cc.match_option("ab", opts) == "ab"
    assert cc.match_option("abc.", opts) == "abc"
    assert cc.match_option("the answer is ab", opts) == "ab"      # not "a"
    assert cc.match_option("abcd", opts) is None                   # a different word


def test_match_option_case_collisions_are_ambiguous_unless_exact():
    opts = ["Yes", "yes"]
    assert cc.match_option("Yes", opts) == "Yes"
    assert cc.match_option("yes", opts) == "yes"
    assert cc.match_option("YES", opts) is None


def test_match_option_multiword_and_unicode():
    opts = ["technical support", "billing", "categoría"]
    assert cc.match_option("Technical Support.", opts) == "technical support"
    assert cc.match_option("sounds like billing to me", opts) == "billing"
    assert cc.match_option("CATEGORÍA", opts) == "categoría"


def test_exact_option_is_strict():
    assert cc.exact_option(" product\n", OPTS) == "product"
    assert cc.exact_option("Product", OPTS) is None
    assert cc.exact_option("the product", OPTS) is None


# ---------------------------------------------------------------------------
# Backend routing
# ---------------------------------------------------------------------------


def test_forced_backend_wins():
    assert cc.resolve_backend("http://x/v1/chat/completions", "llamacpp") == "llamacpp"
    assert cc.resolve_backend("http://x/v1/chat/completions", "ollama") == "ollama"
    assert cc.resolve_backend("http://127.0.0.1:11434/api/chat", "free") == "other"


def test_native_ollama_url_routes_to_ollama():
    assert cc.resolve_backend("http://127.0.0.1:11434/api/chat") == "ollama"


def test_managed_llamacpp_routes_to_llamacpp(monkeypatch):
    import src.model_backend as mb
    monkeypatch.setattr(mb, "serving_backend", lambda url, **k: {"backend": "llamacpp"})
    assert cc.resolve_backend("http://127.0.0.1:8080/v1/chat/completions") == "llamacpp"


def test_unknown_local_server_is_probed_once_and_a_remote_is_never_constrained(monkeypatch):
    import src.model_backend as mb
    calls = []

    def fake(url, probe=True, **k):
        calls.append(probe)
        if "hosted" in url:
            return {"backend": "remote"}
        return {"backend": "llamacpp" if probe else "unknown"}

    monkeypatch.setattr(mb, "serving_backend", fake)
    assert cc.resolve_backend("http://127.0.0.1:9/v1/chat/completions") == "llamacpp"
    assert calls == [False, True]
    assert cc.resolve_backend("https://hosted.example/v1/chat/completions") == "other"


def test_unidentified_server_stays_unconstrained(monkeypatch):
    import src.model_backend as mb
    monkeypatch.setattr(mb, "serving_backend", lambda url, **k: {"backend": "unknown"})
    assert cc.resolve_backend("http://127.0.0.1:9/v1/chat/completions") == "other"


def test_setting_free_disables_detection(monkeypatch):
    monkeypatch.setattr(cc, "_setting", lambda key: "free" if key == "constrained_choice_backend" else True)
    assert cc.resolve_backend("http://127.0.0.1:11434/api/chat") == "other"


# ---------------------------------------------------------------------------
# choose_one against the stub
# ---------------------------------------------------------------------------


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


QUESTION = "Which robot vacuum should I buy for a home with two cats?"  # reference: product


def _prompt(q=QUESTION):
    return f"Classify this research question into exactly ONE category.\n\nQuestion: {q}"


async def test_llamacpp_sends_a_grammar_and_the_answer_is_the_label(monkeypatch):
    async with serve("llamacpp", monkeypatch) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=5)
    assert r.choice == "product" and r.index == 0
    assert r.path == cc.PATH_LLAMACPP and r.backend == "llamacpp"
    assert r.constrained and r.honoured is True and not r.repaired
    assert r.tokens_exact and r.prompt_tokens > 0 and r.completion_tokens > 0
    sent = stub.log[0]
    assert sent["constraint"] == "grammar"
    assert bench.grammar_labels(sent["grammar"]) == OPTS
    assert sent["max_tokens"] >= len("comparison")  # room to finish the longest label
    assert sent["reply"] == "product"


async def test_ollama_sends_a_format_with_an_enum(monkeypatch):
    async with serve("ollama", monkeypatch) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=5)
    assert r.choice == "product"
    assert r.path == cc.PATH_OLLAMA and r.backend == "ollama" and r.honoured is True
    chat = [e for e in stub.log if e["path"] == "/api/chat"][0]
    assert chat["constraint"] == "format"
    assert chat["format"] == {"type": "string", "enum": OPTS}
    assert chat["reply"] == '"product"'                       # a JSON string, no wrapper object


async def test_unconstrained_backend_uses_free_text_and_never_sends_a_grammar(monkeypatch):
    async with serve("plain", monkeypatch) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=5)
    assert r.path == cc.PATH_FREE and not r.constrained and r.honoured is None
    assert r.choice == "product"
    assert all(e["constraint"] == "none" for e in stub.log)


async def test_free_text_reads_a_wordy_reply(monkeypatch):
    # case index 1 of the bench is scripted as "The category is x."
    q, ref = bench.CASES[1]
    async with serve("plain", monkeypatch) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(q), url=url, model="m", timeout=5)
    assert stub.log[0]["reply"].startswith("The category is")
    assert r.choice == ref and not r.repaired and len(stub.log) == 1


async def test_free_text_outside_the_set_gets_exactly_one_repair(monkeypatch):
    q, ref = bench.CASES[5]  # scripted as an invented word on the first try
    async with serve("plain", monkeypatch) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(q), url=url, model="m", timeout=5)
    assert [e["is_repair"] for e in stub.log] == [False, True]
    assert r.repaired and r.choice == ref and r.path == cc.PATH_FREE
    assert len(r.attempts) == 2 and r.attempts[0]["ok"] is False and r.attempts[1]["ok"] is True


async def test_repair_can_be_switched_off(monkeypatch):
    q, _ = bench.CASES[5]
    async with serve("plain", monkeypatch) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(q), url=url, model="m", timeout=5, repair=False)
    assert r.choice is None and r.reason == "unparsed" and len(stub.log) == 1


async def test_still_unparsable_after_the_repair_is_none_never_a_guess(monkeypatch):
    async with serve("plain", monkeypatch, styles=("invented",)) as (stub, url):
        # an unknown question: the stub answers a generic "OK." both times
        r = await cc.choose_one(OPTS, _prompt("something the stub does not know"), url=url, model="m",
                                timeout=5)
    assert r.choice is None and r.index is None and r.reason == "unparsed" and r.repaired
    assert len(stub.log) == 2


async def test_server_that_ignores_the_grammar_is_noticed(monkeypatch):
    q, ref = bench.CASES[1]  # wordy reply
    async with serve("plain", monkeypatch) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(q), url=url, model="m", timeout=5, backend="llamacpp")
    assert stub.log[0]["constraint"] == "grammar"        # it was sent...
    assert r.path == cc.PATH_LLAMACPP and r.constrained
    assert r.honoured is False                            # ...but not honoured
    assert r.choice == ref                                # the lenient reader still found it
    assert cc.stats()["not_honoured"] == 1


async def test_server_that_rejects_the_grammar_falls_back_and_remembers(monkeypatch):
    async with serve("strict", monkeypatch) as (stub, url):
        first = await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=5, backend="llamacpp")
        sent_first = [e["constraint"] for e in stub.log]
        second = await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=5, backend="llamacpp")
    assert first.choice == "product" and first.path == cc.PATH_FREE
    assert first.attempts[0]["status"] == 400 and first.attempts[0]["path"] == cc.PATH_LLAMACPP
    assert sent_first == ["none"]                   # the rejected request never reaches the stub log
    assert second.choice == "product" and second.path == cc.PATH_FREE
    assert len(second.attempts) == 1                # no second grammar attempt
    assert cc.stats()["grammar_rejected"] == 1


async def test_ollama_that_refuses_the_schema_falls_back_to_free_text(monkeypatch):
    async with serve("ollama_strict", monkeypatch) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=5)
    assert r.choice == "product" and r.path == cc.PATH_FREE and not r.constrained
    assert r.attempts[0]["path"] == cc.PATH_OLLAMA and r.attempts[0]["ok"] is False
    assert r.attempts[0]["status"] == 400 and r.attempts[1]["path"] == cc.PATH_FREE


async def test_master_switch_off_never_constrains(monkeypatch):
    monkeypatch.setattr(cc, "_setting", lambda key: False if key == "constrained_choice_enabled" else "auto")
    async with serve("llamacpp", monkeypatch) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=5)
    assert r.path == cc.PATH_FREE and all(e["constraint"] == "none" for e in stub.log)


async def test_timeout_is_reported_and_not_retried(monkeypatch):
    async with serve("llamacpp", monkeypatch, simulate=True, base_ms=1500) as (stub, url):
        r = await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=0.3)
    assert r.choice is None and r.reason == "timeout"
    assert len(r.attempts) == 1


async def test_no_endpoint_is_reported(monkeypatch):
    monkeypatch.setattr(cc, "_resolve_endpoint", lambda *a, **k: (None, None, {}))
    r = await cc.choose_one(OPTS, _prompt())
    assert r.choice is None and r.reason == "no_endpoint" and r.path == cc.PATH_NONE


async def test_programming_errors_raise_but_runtime_failures_do_not(monkeypatch):
    with pytest.raises(ValueError):
        await cc.choose_one([], "x")
    with pytest.raises(ValueError):
        await cc.choose_one(["a"], "   ")
    # a refused connection is a normal failure
    from src import llm_core

    async def refuse(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(llm_core, "llm_call_async", refuse)
    r = await cc.choose_one(OPTS, _prompt(), url="http://127.0.0.1:1/v1/chat/completions", model="m",
                            timeout=1, backend="free", repair=False)
    assert r.choice is None and r.reason == "error" and r.path == cc.PATH_FREE


@pytest.mark.parametrize("mode", ["llamacpp", "ollama", "plain"])
async def test_the_answer_is_never_outside_the_set_on_24_cases(monkeypatch, mode):
    async with serve(mode, monkeypatch) as (stub, url):
        results = []
        for q, ref in bench.CASES:
            results.append((ref, await cc.choose_one(OPTS, _prompt(q), url=url, model="m", timeout=5)))
    for ref, r in results:
        assert r.choice in OPTS
        assert r.choice == ref
    if mode != "plain":
        assert all(r.honoured is True and not r.repaired and len(r.attempts) == 1 for _, r in results)
    stats = cc.stats()
    assert stats["calls"] == 24 and stats["known"] == 24 and stats["unknown"] == 0


async def test_stats_count_paths(monkeypatch):
    async with serve("llamacpp", monkeypatch) as (stub, url):
        await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=5)
        await cc.choose_one(OPTS, _prompt(), url=url, model="m", timeout=5, backend="free")
    s = cc.stats()
    assert s["paths"][cc.PATH_LLAMACPP] == 1 and s["paths"][cc.PATH_FREE] == 1
    assert s["calls"] == 2


def test_sync_wrapper_works_with_and_without_a_running_loop(monkeypatch):
    stub = bench.StubModel("llamacpp", simulate=False)
    server, port = bench.start_stub(stub)
    try:
        url = bench.endpoint_url("llamacpp", port)
        from src import llm_core

        @asynccontextmanager
        async def no_gate(*a, **k):
            yield

        monkeypatch.setattr(llm_core, "_local_model_slot", no_gate)
        monkeypatch.setattr(llm_core, "_get_http_client", lambda: httpx.AsyncClient())
        r = cc.choose_one_sync(OPTS, _prompt(), url=url, model="m", timeout=5)
        assert r.choice == "product"

        async def inside_loop():
            return cc.choose_one_sync(OPTS, _prompt(), url=url, model="m", timeout=5)

        assert asyncio.run(inside_loop()).choice == "product"
        with pytest.raises(ValueError):
            cc.choose_one_sync([], "x")
    finally:
        server.shutdown()
