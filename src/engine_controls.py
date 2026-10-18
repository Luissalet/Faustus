"""Generation controls verified against both the engine and model template."""
from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any

_cache: dict[str, tuple[float, bool]] = {}


def vllm_thinking_budget(base_url: str) -> bool:
    """Probe a configured server's schema; never infer its engine from its IP."""
    base = re.sub(r"/v1(?:/.*)?$", "", base_url.rstrip("/"))
    cached = _cache.get(base)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    supported = False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + "/openapi.json", timeout=1) as response:
            schema = json.load(response)
        fields = schema.get("components", {}).get("schemas", {}).get("ChatCompletionRequest", {}).get("properties", {})
        supported = "thinking_token_budget" in fields and "chat_template_kwargs" in fields
    except Exception:
        pass
    _cache[base] = (time.monotonic(), supported)
    return supported


def glm53_flash(model: str) -> bool:
    normalized = re.sub(r"[_. ]", "-", str(model).lower())
    return bool(re.search(r"(?:^|/)glm-5-3-flash(?:-|$)", normalized))


def controls_for(base_url: str, model: str) -> dict[str, Any] | None:
    if glm53_flash(model) and vllm_thinking_budget(base_url):
        # NVIDIA GLM-5.3-Flash's published template: low / high, else max.
        return {"levels": ["low", "high", "max"], "default": "max",
                "source": "verified_model_template", "engine": "vllm",
                "thinking_supported": True, "thinking_toggle": True,
                "thinking_off_behavior": "low_effort"}
    return None


def apply_controls(payload: dict[str, Any], base_url: str, model: str, *, reasoning_budget: Any = None) -> None:
    caps = controls_for(base_url, model)
    if not caps:
        return
    template = dict(payload.get("chat_template_kwargs") or {})
    thinking = template.get("enable_thinking", True)
    native_effort = template.pop("reasoning_effort", None)
    effort = str(payload.get("reasoning_effort") or native_effort or "max").lower()
    if effort not in caps["levels"]:
        # Fit generic Faustus effort profiles to the model's actual levels.
        effort = "low" if effort in ("none", "minimal", "medium") else "max"
    if not thinking:
        # GLM-5.3-Flash always reasons. Its supported fast path is Low; an
        # artificially empty think block is outside its training template.
        effort = "low"
    template["enable_thinking"] = True
    template.setdefault("clear_thinking", True)
    payload["chat_template_kwargs"] = template
    payload["reasoning_effort"] = effort
    budget = payload.pop("reasoning_budget", None)
    if reasoning_budget is not None:
        budget = reasoning_budget  # an explicit budget does not require a separate think toggle
    if budget is not None:
        # Honor the caller's existing budget, including -1 (unlimited), and
        # leave validation to the engine. Never invent a default cap or
        # replace an explicitly supplied native budget when no override exists.
        payload["thinking_token_budget"] = budget
    # Do not pass llama-server's spelling to vLLM.
    payload.pop("thinking_budget_tokens", None)
