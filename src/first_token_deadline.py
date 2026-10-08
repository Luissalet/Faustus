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
    queue = asyncio.Queue(maxsize=1)

    async def produce():
        # One task owns the generator for its entire lifetime, including
        # cleanup. ContextVar tokens must never be reset in another task.
        try:
            async for chunk in source:
                await queue.put(("chunk", chunk))
            await queue.put(("end", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(("error", exc))
        finally:
            await source.aclose()

    worker = asyncio.create_task(produce())
    try:
        while True:
            try:
                if started:
                    kind, chunk = await queue.get()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    kind, chunk = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                worker.cancel()
                error = {"error": "El modelo no ha empezado a responder dentro del limite de espera. Puedes reducir el contexto, esperar a que se prepare o elegir otro modelo.",
                         "status": 504, "error_class": "first_token_timeout", "retryable": False,
                         "fallback_eligible": False, "attempts": 1, "wait_limit_s": seconds}
                yield "event: error\ndata: " + json.dumps(error, ensure_ascii=False) + "\n\n"
                return
            if kind == "end":
                return
            if kind == "error":
                raise chunk
            started = started or meaningful(chunk)
            yield chunk
    finally:
        if not worker.done() and not worker.cancelling():
            worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
