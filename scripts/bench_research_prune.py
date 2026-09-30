#!/usr/bin/env python3
"""
bench_research_prune.py -- Deep Research page extraction with and without pruning.

Deep Research reads each source by handing the page text to the model. This
script replays exactly that step (`DeepResearcher._fetch_and_extract`) on saved
pages, once with `prune_pages` off (the page as the extractor returns it, up to
`max_content_chars`) and once with it on, and prints, per mode:

* chars read   -- characters of page text placed in the extraction prompt
* time / page  -- wall time of the extraction call (``--model fake`` adds a
                  simulated reading time of chars / ``--sim-rate``, clearly
                  labelled "simulated"; a real endpoint reports measured time)
* sources      -- pages that yielded a finding
* facts        -- sentences in the findings
* cited %      -- share of those sentences found (token containment >= 0.6) in
                  the ORIGINAL page text, i.e. not invented by the model
* gold recall  -- share of the page's gold phrases (queries.json) that appear
                  in the findings: what pruning must not lose

Pages are ``*.html`` files in ``--pages-dir`` (default
``tests/fixtures/research_pages``) with a ``queries.json`` next to them::

    {"page.html": {"query": "what the page is read for", "gold": ["phrase", ...]}}

Usage::

    # deterministic, offline: a stand-in model that copies the prompt sentences
    # matching the query (proves the plumbing and the size reduction)
    python scripts/bench_research_prune.py

    # the real model (any OpenAI-compatible endpoint, e.g. the local server)
    python scripts/bench_research_prune.py --model-name MODEL \\
        --endpoint http://127.0.0.1:8080/v1/chat/completions --timeout 300

    # save real pages once, then measure them repeatedly
    python scripts/bench_research_prune.py --pages-dir D:\\bench_pages \\
        --fetch https://example.org/a https://example.org/b

    --json   print the numbers as JSON instead of a table
    --prune-max-chars 6000   the cap used in the pruned mode
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PAGES_DIR = ROOT / "tests" / "fixtures" / "research_pages"
DEFAULT_SIM_RATE = 150.0     # characters of prompt "read" per second in the simulated mode


def _load_pages(pages_dir: Path) -> List[Dict[str, Any]]:
    queries: Dict[str, Any] = {}
    qfile = pages_dir / "queries.json"
    if qfile.exists():
        queries = json.loads(qfile.read_text(encoding="utf-8"))
    pages = []
    for path in sorted(pages_dir.glob("*.html")):
        meta = queries.get(path.name, {})
        html = path.read_text(encoding="utf-8", errors="replace")
        pages.append({
            "name": path.name,
            "url": f"https://bench.invalid/{path.name}",
            "html": html,
            "query": meta.get("query") or path.stem.replace("_", " "),
            "gold": list(meta.get("gold") or []),
        })
    return pages


def _page_text(html: str, url: str) -> str:
    """What the real fetcher returns as ``content`` for an HTML page."""
    from src.html_markdown import html_to_markdown

    return html_to_markdown(html, base_url=url)["markdown"]


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if len(s.strip()) > 20]


def _contained(sentence: str, reference_tokens: set, threshold: float = 0.6) -> bool:
    from src.research_prune import tokenize

    toks = set(tokenize(sentence))
    if not toks:
        return False
    return len(toks & reference_tokens) / len(toks) >= threshold


class _FakeModel:
    """A deterministic stand-in: answers with the prompt sentences that share
    words with the goal. Latency is simulated from the characters it was given."""

    def __init__(self, sim_rate: float):
        self.sim_rate = max(1.0, sim_rate)
        self.simulated_seconds = 0.0

    async def __call__(self, messages, **kw):
        from src.research_prune import tokenize

        goal_match = re.search(r"Goal: (.*)", messages[0]["content"])
        goal = set(tokenize(goal_match.group(1) if goal_match else ""))
        body = str(messages[-1]["content"])
        self.simulated_seconds += len(body) / self.sim_rate
        scored = []
        for s in _sentences(body):
            overlap = len(goal & set(tokenize(s)))
            if overlap:
                scored.append((overlap, s))
        scored.sort(key=lambda x: -x[0])
        picked = [s for _n, s in scored[:10]]
        if not picked:
            return "{}"
        return json.dumps({"rational": "sentences matching the goal", "evidence": " ".join(picked),
                           "summary": " ".join(picked)})


async def _run_mode(pages: List[Dict[str, Any]], *, prune: bool, args) -> Dict[str, Any]:
    from src.deep_research import DeepResearcher
    from src.research_saturation import finding_facts
    from src.research_prune import fold, tokenize

    fake = None if args.endpoint else _FakeModel(args.sim_rate)
    r = DeepResearcher(
        llm_endpoint=args.endpoint or "http://127.0.0.1:1/v1/chat/completions",
        llm_model=args.model_name or "bench",
        max_content_chars=args.max_content_chars,
        extraction_timeout=args.timeout,
        prune_pages=prune, prune_max_chars=args.prune_max_chars,
    )
    prompt_chars: List[int] = []
    orig_llm = r._llm

    async def _llm(messages, **kw):
        prompt_chars.append(len(str(messages[-1]["content"])))
        return await (fake(messages, **kw) if fake else orig_llm(messages, **kw))

    r._llm = _llm
    by_url = {p["url"]: p for p in pages}

    def fake_fetch(url, timeout=5, **kw):
        p = by_url[url]
        page = {"success": True, "content": _page_text(p["html"], url), "title": p["name"], "og_image": "",
                "headers": {}}
        if kw.get("keep_html"):
            page["raw_html"] = p["html"]
        return page

    import src.search as search_pkg
    real_fetch = search_pkg.fetch_webpage_content
    search_pkg.fetch_webpage_content = fake_fetch
    r._active_search_provider = lambda: "searxng"
    rows = []
    try:
        for p in pages:
            r._url_query[p["url"]] = p["query"]
            before = len(prompt_chars)
            t0 = time.perf_counter()
            finding = await r._fetch_and_extract(p["url"], p["query"], p["name"])
            wall = time.perf_counter() - t0
            facts = finding_facts(finding) if finding else []
            text = _page_text(p["html"], p["url"])
            ref = set(tokenize(text))
            blob = fold(" ".join(str((finding or {}).get(k) or "") for k in ("summary", "evidence")))
            gold_hits = sum(1 for g in p["gold"] if fold(g) in blob)
            rows.append({
                "page": p["name"],
                "chars_read": sum(prompt_chars[before:]),
                "page_chars": len(text),
                "seconds": wall,
                "sources": 1 if finding else 0,
                "facts": len(facts),
                "cited": sum(1 for f in facts if _contained(f, ref)),
                "gold_hits": gold_hits, "gold_total": len(p["gold"]),
            })
    finally:
        search_pkg.fetch_webpage_content = real_fetch
    if fake:
        # the stand-in model is instant; report the simulated reading time instead
        total_sim = fake.simulated_seconds
        for row in rows:
            row["seconds"] = row["chars_read"] / fake.sim_rate
        simulated = True
    else:
        total_sim = sum(row["seconds"] for row in rows)
        simulated = False
    n = max(1, len(rows))
    facts = sum(x["facts"] for x in rows)
    gold_total = sum(x["gold_total"] for x in rows)
    return {
        "mode": "pruned" if prune else "full",
        "simulated_time": simulated,
        "pages": rows,
        "chars_read": sum(x["chars_read"] for x in rows),
        "seconds_per_page": round(total_sim / n, 2),
        "sources": sum(x["sources"] for x in rows),
        "facts": facts,
        "cited_pct": round(100.0 * sum(x["cited"] for x in rows) / facts, 1) if facts else 0.0,
        "gold_recall_pct": round(100.0 * sum(x["gold_hits"] for x in rows) / gold_total, 1) if gold_total else None,
        "trace": list(r.research_trace["pages"]),
    }


def run_benchmark(pages_dir: Path, args) -> Dict[str, Any]:
    pages = _load_pages(Path(pages_dir))
    if not pages:
        raise SystemExit(f"no *.html pages in {pages_dir}")
    full = asyncio.run(_run_mode(pages, prune=False, args=args))
    pruned = asyncio.run(_run_mode(pages, prune=True, args=args))
    saved = 100.0 * (1 - pruned["chars_read"] / full["chars_read"]) if full["chars_read"] else 0.0
    return {"full": full, "pruned": pruned, "chars_saved_pct": round(saved, 1)}


def render(result: Dict[str, Any]) -> str:
    lines = []
    sim = " (simulated reading time)" if result["full"]["simulated_time"] else ""
    lines.append(f"{'mode':8} {'chars read':>11} {'s/page':>8} {'sources':>8} {'facts':>6} {'cited %':>8} {'gold recall %':>14}")
    for key in ("full", "pruned"):
        m = result[key]
        gold = "-" if m["gold_recall_pct"] is None else f"{m['gold_recall_pct']}"
        lines.append(f"{m['mode']:8} {m['chars_read']:>11} {m['seconds_per_page']:>8} {m['sources']:>8} "
                     f"{m['facts']:>6} {m['cited_pct']:>8} {gold:>14}")
    lines.append(f"characters read: -{result['chars_saved_pct']}% with pruning{sim}")
    lines.append("")
    lines.append("per page (full -> pruned chars read):")
    for a, b in zip(result["full"]["pages"], result["pruned"]["pages"]):
        lines.append(f"  {a['page']:28} {a['chars_read']:>7} -> {b['chars_read']:>6}   "
                     f"gold {a['gold_hits']}/{a['gold_total']} -> {b['gold_hits']}/{b['gold_total']}")
    return "\n".join(lines)


def _fetch_pages(urls: List[str], pages_dir: Path) -> None:
    from src.search.content import fetch_webpage_content

    pages_dir.mkdir(parents=True, exist_ok=True)
    qfile = pages_dir / "queries.json"
    queries = json.loads(qfile.read_text(encoding="utf-8")) if qfile.exists() else {}
    for i, url in enumerate(urls, 1):
        page = fetch_webpage_content(url, 15, keep_html=True)
        html = page.get("raw_html")
        if not page.get("success") or not html:
            print(f"skip {url}: {page.get('error') or 'no markup (cached or non-HTML)'}", file=sys.stderr)
            continue
        name = f"page_{i:02d}.html"
        (pages_dir / name).write_text(html, encoding="utf-8")
        queries.setdefault(name, {"query": page.get("title") or url, "gold": [], "source": url})
        print(f"saved {name} <- {url}")
    qfile.write_text(json.dumps(queries, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pages-dir", default=str(DEFAULT_PAGES_DIR))
    ap.add_argument("--endpoint", default="", help="OpenAI-compatible chat completions URL; empty = stand-in model")
    ap.add_argument("--model-name", default="")
    ap.add_argument("--timeout", type=int, default=300, help="per-extraction timeout in seconds (real model)")
    ap.add_argument("--sim-rate", type=float, default=DEFAULT_SIM_RATE, help="simulated characters read per second")
    ap.add_argument("--max-content-chars", type=int, default=15000)
    ap.add_argument("--prune-max-chars", type=int, default=6000)
    ap.add_argument("--fetch", nargs="*", default=None, help="URLs to download into --pages-dir first")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.fetch:
        _fetch_pages(args.fetch, Path(args.pages_dir))
    result = run_benchmark(Path(args.pages_dir), args)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
