"""A total initial-response deadline, including queueing and transport retries."""
from __future__ import annotations

import asyncio
import json
import time


def meaningful(chunk: str) -> bool:
    for line in str(chunk).splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except (ValueError, TypeError):
            continue
        if event.get("delta") or (event.get("type") == "tool_start" and event.get("tool")) or (event.get("type") == "tool_call_delta" and
                                  (event.get("name") or event.get("arg_delta"))):
            return True
    return False


async def bounded(source, seconds: float):
    deadline = time.monotonic() + seconds
    started = False
    try:
        while True:
            try:
                if started:
                    chunk = await anext(source)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    chunk = await asyncio.wait_for(anext(source), timeout=remaining)
            except StopAsyncIteration:
                return
            except TimeoutError:
                error = {"error": "El modelo no ha empezado a responder dentro del limite de espera. Puedes reducir el contexto, esperar a que se prepare o elegir otro modelo.",
                         "status": 504, "error_class": "first_token_timeout", "retryable": False,
                         "fallback_eligible": False, "attempts": 1, "wait_limit_s": seconds}
                yield "event: error\ndata: " + json.dumps(error, ensure_ascii=False) + "\n\n"
                return
            started = started or meaningful(chunk)
            yield chunk
    finally:
        await source.aclose()
