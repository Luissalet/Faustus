"""agent_tools/prune_tools.py -- `page_prune` tool executor.

Thin wrapper over `src.research_prune.prune_page`: give it a URL (fetched with
the same guarded fetcher `web_fetch` uses), raw HTML or already extracted
text, plus the question the page is being read for, and get back only the
blocks that look like content and match the question, with their scores.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 2_000_000


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _render(data: Dict[str, Any], include_blocks: bool) -> str:
    head = (f"Pruned {data['original_chars']} -> {data['pruned_chars']} chars "
            f"({data['blocks_kept']} of {data['blocks_passed']} scored blocks kept, "
            f"{data['blocks_total']} blocks total, top BM25 {data['top_bm25']}, mode {data['mode']}"
            + (f", fallback {data['fallback']}" if data.get("fallback") else "") + ")")
    parts = [head, "", data["text"]]
    if include_blocks and data.get("blocks"):
        parts += ["", "## Block scores (document order; selected = read, passed = above the threshold)"]
        for b in data["blocks"]:
            flag = "KEPT" if b["selected"] else ("pass" if b["passed"] else "drop")
            parts.append(f"- [{flag}] #{b['index']} <{b['tag']}> score {b['score']} bm25 {b['bm25']}: {b['text']}")
    return "\n".join(parts)


class PagePruneTool:
    """`page_prune` {query, url | html | text, max_chars?, threshold?, include_blocks?}."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.research_prune import PruneConfig, prune_page

        args = _args(content)
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "page_prune: provide a query (the question the page is read for)", "exit_code": 1}
        url = str(args.get("url") or "").strip()
        html = args.get("html")
        text = args.get("text")
        html = html if isinstance(html, str) and html.strip() else None
        text = text if isinstance(text, str) and text.strip() else None
        if not (url or html or text):
            return {"error": "page_prune: provide one of url, html or text", "exit_code": 1}

        title = str(args.get("title") or "")
        if url and not (html or text):
            low = url.lower()
            if "://" in low and not low.startswith(("http://", "https://")):
                return {"error": f"page_prune: unsupported URL scheme (only http/https): {url[:80]}", "exit_code": 1}
            if not low.startswith(("http://", "https://")):
                url = "https://" + url
            try:
                from src.search.content import fetch_webpage_content

                page = await asyncio.wait_for(
                    asyncio.to_thread(fetch_webpage_content, url, 10, keep_html=True), timeout=30)
            except asyncio.TimeoutError:
                return {"error": f"page_prune: timed out fetching {url}", "exit_code": 1}
            except Exception as exc:  # noqa: BLE001 - a tool call never crashes the turn
                return {"error": f"page_prune: {url}: {exc}", "exit_code": 1}
            if not page.get("success") or not (page.get("content") or page.get("raw_html")):
                return {"error": f"page_prune: {url}: {page.get('error') or 'no readable content'}", "exit_code": 1}
            html = page.get("raw_html") or None
            text = page.get("content") or None
            title = title or str(page.get("title") or "")

        if (html and len(html) > MAX_INPUT_CHARS) or (text and len(text) > MAX_INPUT_CHARS):
            return {"error": f"page_prune: input larger than {MAX_INPUT_CHARS} characters", "exit_code": 1}

        overrides: Dict[str, Any] = {}
        for key in ("max_chars", "threshold"):
            if args.get(key) is not None:
                overrides[key] = args[key]
        try:
            cfg = PruneConfig.from_settings(
                {"research_prune_max_chars": overrides.get("max_chars", 6000),
                 "research_prune_threshold": overrides.get("threshold", 0.48)})
            result = await asyncio.to_thread(
                prune_page, query, html=html, text=text, title=title,
                original_chars=len(text) if text else None, config=cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("page_prune failed: %s", exc)
            return {"error": f"page_prune: {exc}", "exit_code": 1}

        include_blocks = args.get("include_blocks", True) is not False
        data = result.to_dict(include_blocks=include_blocks, max_blocks=60)
        return {
            "output": _render(data, include_blocks),
            "exit_code": 0,
            "stats": result.trace(),
            "pruned_text": result.text,
            "untrusted_content": True,
        }
