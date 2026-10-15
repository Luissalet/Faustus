"""tests/test_look_at_page.py -- "look at the page" in Deep Research and web_fetch (src/visual_index/lookpage.py).

A fake browser (tests.vi_helpers.fake_web: a Renderers whose web renderer hands back synthetic screenshots) and a fake vision
model stand in for the real ones: no browser opens, no model loads.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.deep_research import DeepResearcher
from src.visual_index import lookpage
from src.visual_index.lookpage import LookBudget, LookResult, assess_page, gap_sentence, gaps_section, look_at_page, pick_evenly

from tests.vi_helpers import BLUE, GREEN, RED, fake_web, page

URL = "https://stats.example.org/report"
LONG = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 40          # ~2.2k chars of prose


def vision_ok(owner):
    return True, "", "qwen-vl"


def vision_none(owner):
    return False, "no vision model is running", ""


class FakeAsk:
    """The vision model: answers per strip index from ``answers`` (default: a line naming the strip)."""

    def __init__(self, answers=None, model="qwen-vl"):
        self.answers, self.model, self.calls = answers or {}, model, []

    def __call__(self, image, prompt, owner=None):
        self.calls.append({"size": image.size, "prompt": prompt})
        n = len(self.calls)
        return {"text": self.answers.get(n, f"| Year | Revenue |\n| 2024 | {n}.2M |"), "model": self.model}


def tall_capture(n_strips=3):
    bands = [(1024, c) for c in (RED, GREEN, BLUE, RED, GREEN, BLUE)[:n_strips]]
    return fake_web({URL: [(0, page(bands, 875))]})


# ------------------------------------------------------------------------------------------------ the decision
def test_assess_page_flags_the_pages_whose_text_is_not_where_the_data_is():
    assert assess_page("", "").reason == "no_text"
    assert assess_page("Short.", "", min_chars=600).reason == "little_text"
    canvas = assess_page("A chart page. " * 60, "<html><canvas id=c></canvas></html>", min_chars=600)
    assert canvas.reason == "canvas" and canvas.visual_signals
    tables = assess_page("Intro text. " * 60, "<table><tr><td>1</td></tr></table>", min_chars=600)
    assert tables.reason == "tables_not_in_text"
    scripted = assess_page("Dashboard loading. " * 50, "<script src='/ag-grid.min.js'></script>", min_chars=600)
    assert scripted.reason == "scripted_data"
    images = assess_page("Figures. " * 100, '<img width="800" height="600"><img width="800"><svg width="900"></svg>', min_chars=600)
    assert images.reason == "image_data"


def test_assess_page_leaves_ordinary_pages_alone():
    assert not assess_page(LONG * 3, "<p>prose</p>", min_chars=600).wants_look
    md_table = "| a | b |\n| 1 | 2 |\n" + LONG
    assert not assess_page(md_table, "<table></table>", min_chars=600).wants_look      # the table came out in the text
    assert not assess_page(LONG * 2, "", min_chars=600, js_rendered=False).wants_look
    # the threshold is configurable
    assert assess_page("x" * 300, "", min_chars=100).reason == ""
    assert assess_page("x" * 300, "", min_chars=1000).reason == "little_text"


def test_a_short_page_with_no_visual_markup_is_not_a_visual_gap():
    quiet = assess_page("Login required.", "<p>Login required.</p>", min_chars=600)
    assert quiet.wants_look and not quiet.visual_signals


def test_budget_is_spent_per_page_and_per_run():
    b = LookBudget(max_pages=2, max_strips=5)
    assert b.take_page(4) == 4
    assert b.take_page(4) == 1                       # only one strip of the run left
    assert b.take_page(4) == 0                       # no pages left
    b2 = LookBudget(max_pages=3, max_strips=10)
    g = b2.take_page(4)
    b2.spent(1)
    b2.give_back(g - 1)
    assert b2.strips_left == 9 and b2.strips_used == 1


def test_pick_evenly_spreads_and_keeps_the_ends():
    picked = pick_evenly(list(range(10)), 3)
    assert picked[0] == 0 and picked[-1] == 9 and len(picked) == 3
    assert pick_evenly([1, 2], 5) == [1, 2]
    assert pick_evenly([1, 2, 3], 1) == [1]


# ------------------------------------------------------------------------------------------------ the look
def run(coro):
    return asyncio.run(coro)


def test_look_reads_each_strip_with_the_question_and_marks_the_result():
    ask = FakeAsk()
    res = run(look_at_page(URL, "What was the 2024 revenue?", renderers=tall_capture(3), ask=ask, vision_check=vision_ok,
                           max_strips=4, timeout_s=20))
    assert res.status == "read" and res.has_text and res.model == "qwen-vl"
    assert res.strips_read == 3 and res.strips_total == 3
    assert len(ask.calls) == 3 and all(c["size"][1] == 1024 for c in ask.calls)
    assert "What was the 2024 revenue?" in ask.calls[0]["prompt"] and URL in ask.calls[0]["prompt"]
    assert "strip 1 of 3" in ask.calls[0]["prompt"]
    assert "Read from a screenshot" in res.marker() and "qwen-vl" in res.marker() and "not extracted text" in res.marker()
    assert "[strip 2/3" in res.text


def test_strips_with_nothing_relevant_are_dropped_and_all_none_is_said():
    ask = FakeAsk({1: "NONE", 2: "None.", 3: "Table: 2024 revenue 7.1M"})
    res = run(look_at_page(URL, "revenue", renderers=tall_capture(3), ask=ask, vision_check=vision_ok, timeout_s=20))
    assert res.status == "read" and "7.1M" in res.text and "NONE" not in res.text and res.strips_read == 3
    none = run(look_at_page(URL, "revenue", renderers=tall_capture(2), ask=FakeAsk({1: "NONE", 2: "NONE"}),
                            vision_check=vision_ok, timeout_s=20))
    assert none.status == "nothing_relevant" and not none.has_text
    assert "none of it bears on the question" in gap_sentence(none, URL)


def test_per_page_cap_reads_only_the_granted_strips_and_says_so():
    ask = FakeAsk()
    budget = LookBudget(max_pages=5, max_strips=20)
    res = run(look_at_page(URL, "q", renderers=tall_capture(6), ask=ask, vision_check=vision_ok, budget=budget, max_strips=2,
                           timeout_s=20))
    assert len(ask.calls) == 2 and res.strips_total == 6 and res.strips_read == 2
    assert "4 strips of the page were not read" in res.note
    assert budget.strips_used == 2 and budget.strips_left == 18 and budget.pages_left == 4


def test_budget_exhausted_reads_nothing():
    ask = FakeAsk()
    spent = LookBudget(max_pages=0, max_strips=10)
    res = run(look_at_page(URL, "q", renderers=tall_capture(2), ask=ask, vision_check=vision_ok, budget=spent, timeout_s=20))
    assert res.status == "budget" and not ask.calls
    assert "limit of screenshots" in gap_sentence(res, URL)


def test_without_a_vision_model_nothing_is_captured_and_nothing_is_invented():
    renderers = tall_capture(2)
    ask = FakeAsk()
    res = run(look_at_page(URL, "q", renderers=renderers, ask=ask, vision_check=vision_none, reason="canvas", timeout_s=20))
    assert res.status == "no_vision" and not res.has_text and res.text == ""
    assert not renderers.calls and not ask.calls                    # the browser was never opened
    en = gap_sentence(res, URL)
    es = gap_sentence(res, URL, "es")
    assert URL in en and "no vision model is available" in en and "no vision model is running" in en
    assert URL in es and "no hay modelo de visión" in es and "tabla" in es


def test_a_model_that_does_not_answer_is_no_vision_not_a_made_up_reading():
    class Silent:
        def __call__(self, image, prompt, owner=None):
            return {"text": "no vision model could answer", "model": ""}

    res = run(look_at_page(URL, "q", renderers=tall_capture(2), ask=Silent(), vision_check=vision_ok, timeout_s=20))
    assert res.status == "no_vision" and not res.has_text


def test_capture_failures_never_raise():
    from src.visual_index.render import RenderError, Renderers

    def boom(url, owner=None, max_tiles=8, **kw):
        raise RenderError("browser unavailable", "browser_unavailable")

    res = run(look_at_page(URL, "q", renderers=Renderers(web=boom), ask=FakeAsk(), vision_check=vision_ok, timeout_s=20))
    assert res.status == "capture_failed" and "browser unavailable" in res.note
    budget = LookBudget(2, 8)
    run(look_at_page(URL, "q", renderers=Renderers(web=boom), ask=FakeAsk(), vision_check=vision_ok, budget=budget, timeout_s=20))
    assert budget.strips_left == 8                                   # nothing was read, nothing is charged


def test_gaps_section_is_in_the_report_language():
    assert gaps_section([]) == ""
    es = gaps_section(["frase"], "es")
    assert "## Páginas con el dato en forma visual" in es and "- frase" in es
    assert "## Pages whose data is visual" in gaps_section(["x"], "en")


# ------------------------------------------------------------------------------------------------ Deep Research
class Run:
    """A DeepResearcher on a fake fetcher, a fake extraction model, a fake browser and a fake vision model."""

    def __init__(self, monkeypatch, *, content="", html="", vision=True, answers=None, strips=3, **kw):
        self.prompts = []
        r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m", max_time=300,
                           look_at_pages=kw.pop("look_at_pages", True), **kw)
        self.r = r
        self.fetches = []
        page_ = {"success": True, "content": content, "title": "Report", "og_image": "", "headers": {}, "raw_html": html}

        def fake_fetch(url, timeout=5, **k):
            self.fetches.append(k)
            return dict(page_)

        async def fake_llm(messages, **k):
            self.prompts.append(messages[-1]["content"])
            return json.dumps({"rational": "r", "evidence": "The 2024 revenue was 2.2M.", "summary": "Revenue for 2024 was 2.2M."})

        monkeypatch.setattr("src.search.fetch_webpage_content", fake_fetch)
        monkeypatch.setattr(r, "_llm", fake_llm)
        monkeypatch.setattr(r, "_active_search_provider", lambda: "searxng")
        r.look_renderers = tall_capture(strips)
        r.look_ask = FakeAsk(answers)
        r.look_vision_check = vision_ok if vision else vision_none
        r.owner = "ada"

    def go(self, url=URL, question="What was the revenue in 2024?"):
        return asyncio.run(self.r._fetch_and_extract(url, question, "Report"))


CANVAS_HTML = "<html><body><canvas id='revenue'></canvas><p>Revenue chart</p></body></html>"


def test_a_canvas_page_is_read_from_a_screenshot_and_cited_as_a_capture(monkeypatch):
    run_ = Run(monkeypatch, content="Revenue chart. Loading data.", html=CANVAS_HTML)
    finding = run_.go()
    assert finding and finding["from_capture"] is True and finding["url"] == URL
    assert finding["capture"]["model"] == "qwen-vl" and finding["capture"]["strips_read"] == 3
    assert finding["evidence"].startswith("[from a screenshot] ")
    prompt = run_.prompts[0]
    assert "Read from a screenshot of the page by the vision model qwen-vl" in prompt and "| 2024 |" in prompt
    assert "UNTRUSTED SOURCE DATA" in prompt                                   # what the model read is page-derived: untrusted
    assert run_.fetches == [{"keep_html": True}]                                # the markup was asked for to judge the DOM
    assert run_.r.look_log[0]["status"] == "read" and not run_.r.visual_gaps
    assert "Screenshots read" in run_.r.get_stats()
    assert "Read from a screenshot" in DeepResearcher._finding_content(finding)


def test_a_page_with_enough_text_is_not_captured(monkeypatch):
    run_ = Run(monkeypatch, content=LONG * 3, html="<p>prose</p>")
    finding = run_.go()
    assert finding and "from_capture" not in finding
    assert not run_.r.look_ask.calls and not run_.r.look_renderers.calls and run_.r.look_log == []


def test_the_look_can_be_switched_off(monkeypatch):
    run_ = Run(monkeypatch, content="Revenue chart.", html=CANVAS_HTML, look_at_pages=False)
    finding = run_.go()
    assert finding and "from_capture" not in finding and not run_.r.look_ask.calls
    assert run_.fetches == [{}]                                                  # and the markup is not even requested


def test_an_empty_text_page_is_still_read_when_it_is_all_picture(monkeypatch):
    run_ = Run(monkeypatch, content="", html="<html><body><img width='900' height='700'></body></html>")
    finding = run_.go()
    assert finding and finding["from_capture"] is True


def test_no_vision_model_records_the_gap_and_invents_nothing(monkeypatch):
    run_ = Run(monkeypatch, content="", html=CANVAS_HTML, vision=False)
    assert run_.go() is None                                                     # no text, no capture: no finding
    assert not run_.r.look_ask.calls and not run_.r.look_renderers.calls
    assert URL in run_.r.visual_gaps and "no vision model" in run_.r.visual_gaps[URL]
    run_.r.report_language = "es"
    run_.r.visual_gaps.clear()
    run_.go()
    assert "no hay modelo de visión" in run_.r.visual_gaps[URL]
    final = run_.r._append_visual_gaps("# Informe\n\nTexto.")
    assert final.startswith("# Informe") and "## Páginas con el dato en forma visual" in final and URL in final
    assert run_.r.get_stats()["Visual pages not read"] == 1


def test_no_vision_with_page_text_still_extracts_the_text_and_reports_the_gap(monkeypatch):
    run_ = Run(monkeypatch, content="Revenue chart. Loading data.", html=CANVAS_HTML, vision=False)
    finding = run_.go()
    assert finding and "from_capture" not in finding and finding["url"] == URL
    assert URL in run_.r.visual_gaps


def test_a_short_plain_page_without_vision_is_not_reported_as_a_visual_gap(monkeypatch):
    run_ = Run(monkeypatch, content="Please sign in to continue to the report page.", html="<p>sign in</p>", vision=False)
    run_.go()
    assert run_.r.visual_gaps == {}


def test_limits_per_page_and_per_run_hold_across_pages(monkeypatch):
    run_ = Run(monkeypatch, content="Revenue chart.", html=CANVAS_HTML, strips=6, look_max_strips_per_page=2,
               look_max_strips_per_run=3, look_max_pages_per_run=8)
    run_.r.look_renderers = fake_web({URL: [(0, page([(1024, c) for c in (RED, GREEN, BLUE, RED, GREEN, BLUE)], 875))],
                                      URL + "2": [(0, page([(1024, c) for c in (RED, GREEN, BLUE)], 875))]})
    first = run_.go()
    assert first["capture"]["strips_read"] == 2
    second = run_.go(URL + "2")
    assert second["capture"]["strips_read"] == 1                                 # only one strip of the run's three was left
    third = run_.go()
    assert "from_capture" not in third and run_.r.visual_gaps[URL].startswith("The page")      # the budget is gone: said so
    assert len(run_.r.look_ask.calls) == 3


def test_the_marker_survives_into_the_sources_the_handler_builds():
    from src.research_handler import ResearchHandler

    findings = [{"url": URL, "title": "Report", "summary": "Revenue for 2024 was 2.2M.", "evidence": "[from a screenshot] x",
                 "from_capture": True, "capture": {"model": "qwen-vl"}},
                {"url": "https://other.example.org/", "title": "Other", "summary": "Plain page summary here.", "evidence": "y"}]
    sources = ResearchHandler._extract_sources(findings)
    assert sources[0]["from_capture"] is True and sources[0]["capture_model"] == "qwen-vl" and "from_capture" not in sources[1]
    assert ResearchHandler._extract_raw_findings(findings)[0]["from_capture"] is True


def test_the_settings_exist_with_the_documented_defaults():
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["research_look_at_pages"] is True and DEFAULT_SETTINGS["web_fetch_look_at_page"] is True
    assert DEFAULT_SETTINGS["research_look_min_chars"] == 600
    for key in lookpage.DEFAULTS:
        assert key in DEFAULT_SETTINGS and DEFAULT_SETTINGS[key] == lookpage.DEFAULTS[key], key


# ------------------------------------------------------------------------------------------------ web_fetch
@pytest.fixture()
def fetcher(monkeypatch):
    from src.agent_tools.web_tools import WebFetchTool

    state = {"result": {"success": True, "content": "", "title": "Report", "raw_html": CANVAS_HTML}, "kw": []}

    def fake_fetch(url, timeout=5, max_bytes=None, keep_html=False, **k):
        state["kw"].append({"keep_html": keep_html})
        return dict(state["result"])

    monkeypatch.setattr("src.search.content.fetch_webpage_content", fake_fetch)
    state["tool"] = WebFetchTool()
    ask = FakeAsk()
    original = lookpage.look_at_page

    async def patched(url, question, **kw):
        kw.update(renderers=state.get("renderers") or tall_capture(2), ask=ask,
                  vision_check=state.get("vision") or vision_ok, timeout_s=20)
        state["question"] = question
        return await original(url, question, **kw)

    monkeypatch.setattr(lookpage, "look_at_page", patched)
    state["ask"] = ask
    return state


def call(state, args):
    return asyncio.run(state["tool"].execute(json.dumps(args), {"owner": "ada"}))


def test_web_fetch_reads_a_screenshot_when_the_text_is_empty(fetcher):
    out = call(fetcher, {"url": URL, "question": "revenue 2024"})
    assert out["exit_code"] == 0 and out["from_capture"] is True and out["look"]["status"] == "read"
    assert "Read from a screenshot of the page by the vision model qwen-vl" in out["output"] and "| 2024 |" in out["output"]
    assert fetcher["question"] == "revenue 2024" and fetcher["kw"] == [{"keep_html": True}]
    assert len(out["evidence_refs"]) == 1 and "screenshot" in out["evidence_refs"][0]["locator"]["value"]


def test_web_fetch_appends_the_capture_after_the_text(fetcher):
    fetcher["result"] = {"success": True, "content": "Revenue chart. Loading.", "title": "Report", "raw_html": CANVAS_HTML}
    out = call(fetcher, {"url": URL})
    assert out["output"].index("Revenue chart. Loading.") < out["output"].index("Read from a screenshot")
    assert len(out["evidence_refs"]) == 2


def test_web_fetch_with_look_false_is_the_original_behaviour(fetcher):
    out = call(fetcher, {"url": URL, "look": False})
    assert out["exit_code"] == 1 and "no readable text content" in out["error"]
    assert not fetcher["ask"].calls and fetcher["kw"] == [{"keep_html": False}]


def test_web_fetch_ordinary_pages_are_not_captured(fetcher):
    fetcher["result"] = {"success": True, "content": LONG * 3, "title": "Blog", "raw_html": "<p>prose</p>"}
    out = call(fetcher, {"url": URL})
    assert out["exit_code"] == 0 and "look" not in out and not fetcher["ask"].calls


def test_web_fetch_look_true_forces_it_even_on_a_text_page(fetcher):
    fetcher["result"] = {"success": True, "content": LONG * 3, "title": "Blog", "raw_html": "<p>prose</p>"}
    out = call(fetcher, {"url": URL, "look": True})
    assert out["from_capture"] is True and LONG[:40] in out["output"]


def test_web_fetch_without_a_vision_model_says_so_and_does_not_guess(fetcher):
    fetcher["vision"] = vision_none
    out = call(fetcher, {"url": URL})
    assert out["exit_code"] == 1 and "no vision model is available" in out["error"] and out["look"]["status"] == "no_vision"
    fetcher["result"] = {"success": True, "content": "Revenue chart. Loading.", "title": "Report", "raw_html": CANVAS_HTML}
    out = call(fetcher, {"url": URL})
    assert out["exit_code"] == 0 and "[note: The page" in out["output"] and "from_capture" not in out


def test_web_fetch_schema_accepts_look_and_question():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    spec = next(s for s in FUNCTION_TOOL_SCHEMAS if (s.get("function") or s).get("name") == "web_fetch")
    props = (spec.get("function") or spec)["parameters"]["properties"]
    assert props["look"]["type"] == "boolean" and props["question"]["type"] == "string"
