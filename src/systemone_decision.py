"""Local Ollaya's native multi-question protocol, mapped to Faustus decisions.

Independent adapter based on the published /v1/systemone contract. Confidence
is a model estimate; unknown/malformed answers remain advisory abstentions.
"""
from __future__ import annotations

import asyncio
import math
import time
from urllib.parse import urlsplit

import httpx


def local_base(url: str) -> str:
    p = urlsplit(str(url).strip())
    if p.scheme not in ("http", "https") or p.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Ollaya must use an explicit loopback HTTP(S) endpoint")
    if p.username or p.password or p.query or p.fragment or p.path.rstrip("/") not in ("", "/v1", "/v1/systemone"):
        raise ValueError("invalid Ollaya base URL")
    return p.scheme + "://" + p.netloc


def _canonical(name: str) -> str:
    return name.removesuffix(":latest")


async def decide_native(context, fields, *, budget, min_confidence, instructions="", owner=None):
    from src import typed_decision as td
    from src.privacy_policy import assert_outbound
    started = time.monotonic()
    model = str(td._setting("typed_decision_ollaya_model") or "").strip()
    try:
        base = local_base(td._setting("typed_decision_ollaya_url"))
        assert_outbound("typed_decision", base, owner=owner)
        if not model:
            raise ValueError("model is required")
    except Exception:
        return {f.name: td._unavailable(f, "invalid_local_endpoint", model) for f in fields}
    if len(context) > td.MAX_CONTEXT_CHARS:
        return {f.name: td._unavailable(f, "context_too_long", model) for f in fields}
    names = [f.name for f in fields]
    if len(names) != len(set(names)) or len(fields) > 256 or any(len(set(f.options())) < 2 for f in fields):
        return {f.name: td._unavailable(f, "invalid_questions", model) for f in fields}
    questions = {}
    for f in fields:
        boolean = isinstance(f.choices, str) and f.choices.strip().lower() == "bool"
        criteria = ({"true": f.description_for(0, "yes") or "The statement is true",
                     "false": f.description_for(1, "no") or "The statement is false"}
                    if boolean else {option: f.description_for(i, option) or option for i, option in enumerate(f.options())})
        questions[f.name] = {"type": "noul" if boolean else "choice",
            "instructions": (instructions + "\n" + f.question).strip(),
            "criteria": criteria}
    try:
        kwargs = {"timeout": httpx.Timeout(budget, connect=min(budget, 1.0)), "trust_env": False}
        if td._TRANSPORT is not None:
            kwargs["transport"] = td._TRANSPORT
        async with httpx.AsyncClient(**kwargs) as client:
            if not td._setting("typed_decisions_may_load"):
                response = await client.get(base + "/api/ps")
                response.raise_for_status()
                resident = response.json().get("models", [])
                if not any(_canonical(str(row.get("name") or row.get("model") or "")) == _canonical(model) for row in resident):
                    return {f.name: td._unavailable(f, "model_not_resident", model) for f in fields}
            response = await client.post(base + "/v1/systemone", json={"model": model, "state": context, "questions": questions})
            response.raise_for_status()
            data = response.json()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = "timeout" if "timeout" in type(exc).__name__.lower() else "error"
        return {f.name: td._unavailable(f, reason, model) for f in fields}
    answers = data.get("answers", {}) if isinstance(data, dict) else {}
    actual_model = str(data.get("model") or model) if isinstance(data, dict) else model
    out = {}
    for fld in fields:
        answer = answers.get(fld.name, {}) if isinstance(answers, dict) else {}
        decision = td._unavailable(fld, "malformed_answer", actual_model)
        try:
            if questions[fld.name]["type"] == "noul":
                probability = answer["noul"]
                if answer.get("type") != "noul" or isinstance(probability, bool) or not isinstance(probability, (int, float)):
                    raise ValueError("wrong boolean schema")
                probability = float(probability)
                if not math.isfinite(probability) or not 0 <= probability <= 1:
                    raise ValueError("invalid boolean probability")
                dist = {"yes": probability, "no": 1 - probability}
                best = max(dist, key=dist.get)
                answer = {"type": "choice", "choice": best, "probabilities": dist,
                          "confidence": dist[best]}
            probabilities = answer["probabilities"]
            options = fld.options()
            if answer.get("type") != "choice" or set(probabilities) != set(options):
                raise ValueError("wrong choice schema")
            dist = {option: float(probabilities[option]) for option in options}
            confidence = float(answer["confidence"])
            if any(isinstance(p, bool) or not isinstance(p, (int, float)) for p in [*probabilities.values(), answer["confidence"]]):
                raise ValueError("non-numeric probability")
            if any(not math.isfinite(p) or not 0 <= p <= 1 for p in [*dist.values(), confidence]):
                raise ValueError("invalid probability")
            if abs(sum(dist.values()) - 1.0) > 0.005:
                raise ValueError("probabilities do not sum to one")
            best = max(dist, key=dist.get)
            if answer.get("choice") != best:
                raise ValueError("choice disagrees with probabilities")
            confidence = min(confidence, dist[best])
            known = confidence >= min_confidence and list(dist.values()).count(dist[best]) == 1
            decision = td.Decision(field=fld.name, value=best if known else None, best=best,
                                   confidence=confidence, mass=1.0, distribution=dist,
                                   method="systemone", reason="" if known else "low_confidence",
                                   ms=(time.monotonic() - started) * 1000, model=actual_model)
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        out[fld.name] = decision
    return out
