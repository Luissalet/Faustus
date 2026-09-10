import asyncio
import inspect
import json
from typing import Dict, Any

from src.constants import MAX_OUTPUT_CHARS

class WebSearchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.search import comprehensive_web_search
        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        raw = content.strip()
        query = raw
        time_filter = None
        max_pages = 5
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "query" in parsed:
                    query = str(parsed.get("query", "")).strip()
                    tf = parsed.get("time_filter") or parsed.get("freshness")
                    if isinstance(tf, str) and tf.lower() in ("day", "week", "month", "year"):
                        time_filter = tf.lower()
                    mp = parsed.get("max_pages")
                    if isinstance(mp, int) and 1 <= mp <= 10:
                        max_pages = mp
            except json.JSONDecodeError:
                pass
        if not query:
            query = raw.split("\n")[0].strip()
        if time_filter is None:
            q_lc = query.lower()
            if any(kw in q_lc for kw in ("today", "latest", "breaking", "this morning", "right now", "currently")):
                time_filter = "day"
            elif any(kw in q_lc for kw in ("this week", "past week", "recent news", "last few days")):
                time_filter = "week"
            elif any(kw in q_lc for kw in ("this month", "past month")):
                time_filter = "month"
            elif " news" in q_lc or q_lc.startswith("news ") or q_lc.endswith(" news"):
                time_filter = "week"
        loop = asyncio.get_running_loop()
        if progress_cb:
            await progress_cb({
                "elapsed_s": 0,
                "tail": f"Searching web for: {query[:160]}",
            })
        try:
            text, sources = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: comprehensive_web_search(
                        query,
                        max_pages=max_pages,
                        time_filter=time_filter,
                        return_sources=True,
                    ),
                ),
                timeout=30,
            )
        except asyncio.TimeoutError:
            return {
                "error": f"web_search timed out after 30s: {query[:200]}",
                "exit_code": 1,
            }
        except Exception as e:
            return {
                "error": f"web_search failed: {type(e).__name__}: {str(e) or 'no details'}",
                "exit_code": 1,
                "untrusted_content": True,
            }
        if progress_cb:
            await progress_cb({
                "elapsed_s": 30,
                "tail": "Search completed; preparing sources.",
            })
        output = text[:MAX_OUTPUT_CHARS] if len(text) > MAX_OUTPUT_CHARS else text
        if sources:
            output += "\n\n<!-- SOURCES:" + json.dumps(sources) + " -->"
        return {"output": output, "exit_code": 0}

def _evidence_for_fetch(url: str, text: str, *, owner_id: str, project_id, truncated: bool,
                         fetched_bytes, total_bytes):
    """EvidenceRef for a web_fetch result (WEB-02): URL, content hash, and

    capture time, so a caller can later tell whether the page it is citing
    still matches what was actually read -- the same idea
    `context_ledger.evidence_for_read` already gives file reads, applied to
    the web instead of duplicating a second evidence shape for it (rule 4).
    `locator` is `byte_range` when the download budget cut the body short
    (the hash only covers what was kept, never the whole page) and `whole`
    otherwise, so a stale/partial fetch is distinguishable from a complete one.
    """
    import hashlib

    from src.contracts import EvidenceLocator, EvidenceRef
    from src.contracts.base import now_iso

    digest = hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()
    evidence_id = "evi_web_" + hashlib.sha256(
        f"{url}:{digest}".encode("utf-8", "replace")
    ).hexdigest()[:24]
    if truncated:
        locator = EvidenceLocator(kind="byte_range", value=f"0-{int(fetched_bytes or 0)}")
    else:
        locator = EvidenceLocator(kind="whole", value=str(url)[:512])
    return EvidenceRef(
        evidence_id=evidence_id,
        owner_id=owner_id or "system",
        project_id=project_id,
        source_type="web",
        source_ref=str(url)[:512],
        source_revision=digest[:16],
        content_sha256=digest,
        captured_at=now_iso(),
        locator=locator,
        derived_from=(),
        retention="task",
    )


class WebFetchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.search.content import fetch_webpage_content
        from src.constants import WEB_FETCH_HARD_MAX_BYTES
        raw = content.strip()
        url = ""
        max_bytes = None
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    url = str(parsed.get("url") or "").strip()
                    # Download-budget override (#3812): "full": true raises the
                    # budget to the hard cap; an explicit max_bytes is clamped
                    # to the hard cap downstream. Default stays the soft cap.
                    if parsed.get("full") is True:
                        max_bytes = WEB_FETCH_HARD_MAX_BYTES
                    mb = parsed.get("max_bytes")
                    if isinstance(mb, int) and mb > 0:
                        max_bytes = mb
            except json.JSONDecodeError:
                url = ""
        if not url:
            url = raw.split("\n")[0].strip()
        if not url or url.startswith("{") or any(c in url for c in (" ", "\t", "\n")):
            return {"error": "web_fetch: provide a single URL or domain, e.g. example.com", "exit_code": 1}
        low = url.lower()
        if "://" in low and not low.startswith(("http://", "https://")):
            return {"error": f"web_fetch: unsupported URL scheme (only http/https): {url[:80]}", "exit_code": 1}
        if not low.startswith(("http://", "https://")):
            url = "https://" + url
        loop = asyncio.get_running_loop()
        try:
            def _fetch():
                kwargs = {"timeout": 10}
                try:
                    sig = inspect.signature(fetch_webpage_content)
                    if "max_bytes" in sig.parameters:
                        kwargs["max_bytes"] = max_bytes
                except (TypeError, ValueError):
                    # Some deployed/test shims may not expose a signature.
                    # Prefer compatibility over failing the whole fetch.
                    pass
                return fetch_webpage_content(url, **kwargs)

            result = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch),
                timeout=30,
            )
        except asyncio.TimeoutError:
            return {"error": f"web_fetch: timed out fetching {url}", "exit_code": 1}
        except Exception as e:
            return {"error": f"web_fetch: {url}: {e}", "exit_code": 1}
        err = result.get("error")
        text = (result.get("content") or "").strip()
        title = result.get("title") or ""

        if not text:
            if err:
                return {
                    "error": f"web_fetch: {url}: {err}",
                    "exit_code": 1,
                    "untrusted_content": True,
                }
            return {"error": f"web_fetch: {url}: no readable text content (not HTML, or the page needs JS/login)", "exit_code": 1}

        # Tell the model when the download budget cut the body short and how
        # to get the rest, instead of silently presenting a partial page as
        # the whole thing.
        size_note = ""
        if result.get("truncated"):
            fetched = result.get("fetched_bytes") or 0
            total = result.get("total_bytes")
            total_txt = f" of {total:,} bytes" if total else ""
            size_note = (
                f"[partial content: download stopped at {fetched:,} bytes{total_txt}. "
                f'Re-call with {{"url": "{url}", "full": true}} to fetch up to '
                f"{WEB_FETCH_HARD_MAX_BYTES:,} bytes.]\n\n"
            )

        # The notice must lead the output so the MAX_OUTPUT_CHARS trim below can
        # never drop it. The title is untrusted, uncapped page content, so a
        # giant title ahead of the notice could push it out of range; keep the
        # notice first and cap the title as a second guard.
        if len(title) > 300:
            title = title[:300] + "..."
        header = (f"# {title}\n" if title else "") + f"Source: {url}\n\n"
        output = size_note + header + text
        download_truncated = bool(result.get("truncated"))
        fetched_bytes = result.get("fetched_bytes")
        total_bytes = result.get("total_bytes")

        output_truncated = len(output) > MAX_OUTPUT_CHARS
        if output_truncated:
            output = output[:MAX_OUTPUT_CHARS] + "\n\n[...truncated]"

        # WEB-02: declared, never silent -- two independent truncation points
        # (the download budget in fetch_webpage_content, and the output-char
        # cap here) collapse into one explicit `truncated`/`kept_bytes` pair
        # instead of the caller having to notice a "[...truncated]" string.
        response: Dict[str, Any] = {"output": output, "exit_code": 0}
        response["truncated"] = download_truncated or output_truncated
        if download_truncated:
            response["kept_bytes"] = fetched_bytes
        elif output_truncated:
            response["kept_bytes"] = len(output)

        # EvidenceRef (WEB-02): URL, content hash, capture time -- best-effort,
        # must never turn a successful fetch into a failure (mirrors
        # ReadFileTool's own evidence bookkeeping in filesystem_tools.py).
        try:
            owner_id = str(ctx.get("owner") or "") if isinstance(ctx, dict) else ""
            project_id = (str(ctx.get("project_id") or "") or None) if isinstance(ctx, dict) else None
            evidence = _evidence_for_fetch(
                url, text,
                owner_id=owner_id or "system",
                project_id=project_id,
                truncated=download_truncated,
                fetched_bytes=fetched_bytes,
                total_bytes=total_bytes,
            )
            response["evidence_refs"] = [evidence.to_mapping()]
        except Exception:
            pass

        return response
