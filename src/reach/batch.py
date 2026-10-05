"""Bounded URL fan-out with independent results and stable input order."""
from __future__ import annotations

import asyncio
from pydantic import BaseModel, ConfigDict, Field

from src.reach.base import ReachResult


class BatchReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    urls: list[str] = Field(min_length=1, max_length=100)
    concurrency: int = Field(default=4, ge=1, le=8)
    timeout_s: int = Field(default=20, ge=1, le=60)


async def read_many(request: BatchReadRequest, *, ctx: dict | None = None) -> dict:
    from src.reach import router
    urls = list(dict.fromkeys(url.strip() for url in request.urls))
    if any(not url for url in urls):
        raise ValueError("batch URLs must not be empty")
    semaphore = asyncio.Semaphore(request.concurrency)

    async def one(url):
        async with semaphore:
            try:
                result = await asyncio.wait_for(router.read(url, ctx=ctx or {}), request.timeout_s)
            except asyncio.TimeoutError:
                result = ReachResult(channel=router.detect_channel(url), url=url, error="source_timeout")
            except Exception as exc:
                result = ReachResult(channel=router.detect_channel(url), url=url, error=type(exc).__name__)
            return {"requested_url": url, **result.to_dict()}

    results = await asyncio.gather(*(one(url) for url in urls))
    ok = sum(not row["error"] for row in results)
    return {"results": results, "requested_count": len(request.urls), "unique_count": len(urls),
            "succeeded": ok, "failed": len(results) - ok, "untrusted_content": True}
