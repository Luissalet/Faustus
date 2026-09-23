#!/usr/bin/env python3
"""scripts/eval_typed_decision.py — how good are typed decisions, really?

Runs the labelled cases in ``docs/evals/typed_decision_cases.json`` through
the exact prompts the product uses and reports, per task:

* accuracy of the RULE alone, the TYPED decision alone (raw argmax on every
  case) and the COMBINED behaviour the product ships (rule first; decision
  only where the rule is unsure / the entity is untyped, and only above the
  confidence and mass thresholds);
* how often the combined behaviour flipped the rule, and how many of those
  flips were right;
* calibration buckets (stated confidence vs observed accuracy);
* p50/p95 latency of the first field on a context and of the following
  field on the SAME context (the shared-prefix / prompt-cache effect), plus
  the cached prompt tokens the server reports when it reports them.

Tasks:

* ``freshness`` — does answering need information that changes over time?
  Two fields per message: the product's question first, then a trivial
  "is this written in Spanish?" on the same prefix (its accuracy is a sanity
  check, its latency is the cache measurement).
* ``entity_types`` — which of the brain's entity types a named thing is in
  its sentence, with the same field the brain's typing pass asks.

Endpoints:

    # an explicit OpenAI-compatible server (e.g. a loopback helper)
    python scripts/eval_typed_decision.py --url http://127.0.0.1:8082/v1 --model helper
    # a local Ollama (native /api/chat is used automatically)
    python scripts/eval_typed_decision.py --url http://127.0.0.1:11434 --model some-model:8b
    # whatever the Utility model setting resolves to on this install
    python scripts/eval_typed_decision.py
    # offline, deterministic, for CI
    python scripts/eval_typed_decision.py --fake

A model that is not already resident is NOT loaded unless ``--allow-load``
is given (the same etiquette as the product). ``--json`` prints the full
report; ``--markdown`` prints a table ready to paste into
``docs/evals/typed-decisions.md``.
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import typed_decision as td  # noqa: E402

DEFAULT_CASES = REPO_ROOT / "docs" / "evals" / "typed_decision_cases.json"
FAKE_URL = "http://fake-model.test/v1/chat/completions"
LANG_QUESTION = "Is the message written in Spanish?"


# ---------------------------------------------------------------------------
# A deterministic fake model (for --fake / CI): keyword cues in, letter
# log-probabilities out, in the OpenAI-compatible shape. It also mimics a
# prompt cache: it remembers the previous prompt and reports how many
# leading characters were shared as "cached tokens".
# ---------------------------------------------------------------------------

_FAKE_FRESH = re.compile(
    r"\b(ahora|hoy|actual\w*|reciente|[uú]ltim[oa]s?|esta semana|este mes|este a[nñ]o|temporada|"
    r"now|today|current\w*|latest|these days|this (?:week|month|morning)|tonight|last night|"
    r"precio|vale|cu[aá]nto est[aá]|price|worth|bitcoin|d[oó]lar|bolsa|headlines|noticias|"
    r"gan[oó]|won|win|standings|liga|goleador|record|tiempo har[aá]|weather|"
    r"version|versi[oó]n|release|presid\w*|coach|entrena\w*|runs|abiert[oa]|open|tickets|"
    r"huelga|queue|habitantes|population|eclipse|20[2-9]\d)\b",
    re.IGNORECASE,
)
_FAKE_TIMELESS = re.compile(
    r"\b(explica\w*|explain|traduce|translate|resume|summari[sz]e|haiku|escribe|write|poema|poem|"
    r"verso|paragraph|p[aá]rrafo|circuit|ohm|my|mi|calendar|calendario)\b",
    re.IGNORECASE,
)
_FAKE_TYPES: Sequence[Tuple[str, str]] = (
    ("event", r"\b(feria|summit|boda|hackathon|marat[oó]n|congreso|conference|festival)\b"),
    ("organization", r"\b(labs|logistics|grupo|traders|cf|inc|club|works at|trabajo en|firm[oó])\b"),
    ("project", r"\b(project|proyecto|iniciativa|migration|rewrite|beta)\b"),
    ("tool", r"\b(uso|use|with|migrated the build|instal[oó]|programa en|format)\b"),
    ("place", r"\b(vive en|moving to|located in|en isla|verano en|ser[aá] en|del r[ií]o)\b"),
    ("concept", r"\b(estudiando|covered|interesa|thesis|curso trata)\b"),
    ("person", r"\b(con|with|me recomend[oó]|join the call|sister|lunch)\b"),
)


def _fake_answer(user: str) -> Tuple[str, float]:
    context = user.split('"""')[1] if user.count('"""') >= 2 else user
    tail = user.split('"""')[-1]
    letters = re.findall(r"^([A-Z])\) ([^\n—]+)", tail, re.MULTILINE)
    options = [o.strip() for _, o in letters]
    spread = 0.55 + (sum(map(ord, context)) % 40) / 100.0  # deterministic 0.55..0.94
    if LANG_QUESTION in tail:
        spanish = bool(re.search(r"[¿¡ñáéíóú]|\b(el|la|de|qu[eé]|es|en|un|una)\b", context, re.I))
        return ("A" if spanish else "B"), 0.97
    if "kind of thing" in tail:
        for etype, pattern in _FAKE_TYPES:
            if re.search(pattern, context, re.IGNORECASE) and etype in options:
                return "ABCDEFGH"[options.index(etype)], spread
        return "ABCDEFGH"[options.index("other")] if "other" in options else "A", 0.5
    fresh = bool(_FAKE_FRESH.search(context)) and not _FAKE_TIMELESS.search(context)
    return ("A" if fresh else "B"), spread


class _FakeModel:
    def __init__(self) -> None:
        self.previous = ""

    def __call__(self, request):
        import httpx
        payload = json.loads(request.content.decode("utf-8"))
        prompt = "".join(m.get("content", "") for m in payload.get("messages", []))
        shared = len(os.path.commonprefix([prompt, self.previous]))
        self.previous = prompt
        time.sleep(max(0, len(prompt) - shared) * 4e-6)  # prefill of the uncached part
        user = payload["messages"][-1]["content"]
        letter, p = _fake_answer(user)
        others = [chr(ord("A") + i) for i in range(8) if chr(ord("A") + i) != letter]
        rest = max(1e-6, (1.0 - p - 0.03) / len(others))
        import math
        top = [{"token": letter, "logprob": math.log(p)}]
        top += [{"token": o, "logprob": math.log(rest)} for o in others]
        top.append({"token": "The", "logprob": math.log(0.03)})
        body = {"choices": [{"message": {"role": "assistant", "content": letter},
                             "logprobs": {"content": [{"token": letter, "logprob": math.log(p),
                                                       "top_logprobs": top}]}}],
                "usage": {"prompt_tokens": len(prompt) // 4,
                          "prompt_tokens_details": {"cached_tokens": shared // 4}}}
        return httpx.Response(200, json=body)


# ---------------------------------------------------------------------------
# Calling the model with the product's own prompt pieces
# ---------------------------------------------------------------------------

def _cached_tokens(data: Any) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    timings = data.get("timings") or {}
    if isinstance(timings, dict) and isinstance(timings.get("cache_n"), int):
        return timings["cache_n"]
    details = (data.get("usage") or {}).get("prompt_tokens_details") or {}
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return details["cached_tokens"]
    if isinstance(data.get("prompt_eval_count"), int) and isinstance(data.get("prompt_eval_cached_count"), int):
        return data["prompt_eval_cached_count"]
    return None


class Runner:
    def __init__(self, url: str, model: str, wire: str, headers: Dict[str, str], timeout_s: float):
        self.url, self.model, self.wire, self.headers, self.timeout_s = url, model, wire, headers, timeout_s

    async def ask(self, context: str, fld: "td.Field") -> Tuple["td.Decision", Optional[int]]:
        messages = td.build_messages(context, fld)
        payload = td.build_payload(self.wire, self.model, messages, len(fld.options()), self.url)
        t0 = time.monotonic()
        try:
            data = await asyncio.wait_for(td._post(self.url, payload, self.headers, self.timeout_s),
                                          timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001
            ms = (time.monotonic() - t0) * 1000
            reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else f"error: {type(exc).__name__}"
            return td.Decision(field=fld.name, value=None, confidence=None, mass=None,
                               method="unavailable", ms=ms, reason=reason, model=self.model), None
        ms = (time.monotonic() - t0) * 1000
        # raw argmax (thresholds 0): the report applies the product's thresholds itself
        decision = td.interpret(fld, data, self.wire, min_confidence=0.0, min_mass=0.0,
                                ms=ms, model=self.model)
        return decision, _cached_tokens(data)


def _accepted(d: "td.Decision", min_conf: float, min_mass: float) -> bool:
    return (d.method == "logprobs" and d.value is not None and d.confidence is not None
            and d.mass is not None and d.confidence >= min_conf and d.mass >= min_mass)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

async def run_freshness(runner: Runner, cases: List[Dict[str, Any]], min_conf: float,
                        min_mass: float) -> List[Dict[str, Any]]:
    from src.freshness import FRESHNESS_QUESTION, freshness_assessment
    main = td.Field("needs_web", FRESHNESS_QUESTION, "bool")
    lang = td.Field("spanish", LANG_QUESTION, "bool")
    rows = []
    for case in cases:
        text = case["text"]
        assessment = freshness_assessment(text)
        d1, c1 = await runner.ask(text, main)
        d2, c2 = await runner.ask(text, lang)
        rule = bool(assessment["time_sensitive"])
        combined = rule
        if not assessment["confident"] and _accepted(d1, min_conf, min_mass):
            combined = d1.value == "yes"
        rows.append({
            "text": text, "lang": case.get("lang"), "label": bool(case["label"]),
            "rule": rule, "rule_confident": bool(assessment["confident"]), "why": assessment["why"],
            "typed": None if d1.value is None else d1.value == "yes",
            "confidence": d1.confidence, "mass": d1.mass, "method": d1.method, "reason": d1.reason,
            "accepted": _accepted(d1, min_conf, min_mass), "combined": combined,
            "ms_first": d1.ms, "ms_second": d2.ms, "cached_first": c1, "cached_second": c2,
            "lang_ok": (d2.value == ("yes" if case.get("lang") == "es" else "no")) if d2.value else None,
        })
    return rows


async def run_entity_types(runner: Runner, cases: List[Dict[str, Any]], min_conf: float,
                           min_mass: float) -> List[Dict[str, Any]]:
    from src.brain.extract import entity_type_field
    rows = []
    for case in cases:
        d, cached = await runner.ask(case["sentence"], entity_type_field(case["name"]))
        combined = d.value if (_accepted(d, min_conf, min_mass) and d.value != "other") else "other"
        rows.append({
            "sentence": case["sentence"], "name": case["name"], "lang": case.get("lang"),
            "label": case["type"], "rule": "other", "typed": d.value, "confidence": d.confidence,
            "mass": d.mass, "method": d.method, "reason": d.reason,
            "accepted": _accepted(d, min_conf, min_mass), "combined": combined,
            "ms_first": d.ms, "cached_first": cached,
        })
    return rows


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _pct(values: Sequence[float], q: float) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return round(vals[k], 1)


def _acc(rows: Sequence[Dict[str, Any]], key: str) -> Optional[float]:
    if not rows:
        return None
    return round(sum(1 for r in rows if r[key] == r["label"]) / len(rows), 4)


def calibration(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    edges = [0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0001]
    out = []
    for lo, hi in zip(edges, edges[1:]):
        bucket = [r for r in rows if r["method"] == "logprobs" and r["confidence"] is not None
                  and lo <= r["confidence"] < hi]
        if not bucket:
            continue
        right = sum(1 for r in bucket if r["typed"] == r["label"])
        out.append({"bucket": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(bucket),
                    "mean_confidence": round(sum(r["confidence"] for r in bucket) / len(bucket), 3),
                    "accuracy": round(right / len(bucket), 3)})
    return out


def summarize(rows: List[Dict[str, Any]], *, cache: bool) -> Dict[str, Any]:
    flips = [r for r in rows if r["combined"] != r["rule"]]
    accepted = [r for r in rows if r["accepted"]]
    methods: Dict[str, int] = {}
    for r in rows:
        methods[r["method"]] = methods.get(r["method"], 0) + 1
    out: Dict[str, Any] = {
        "n": len(rows),
        "accuracy": {"rule": _acc(rows, "rule"), "typed": _acc(rows, "typed"),
                     "combined": _acc(rows, "combined")},
        "typed_when_accepted": _acc(accepted, "typed"),
        "accepted": len(accepted),
        "flips": len(flips),
        "flips_right": sum(1 for r in flips if r["combined"] == r["label"]),
        "methods": methods,
        "calibration": calibration(rows),
        "latency_ms": {"first_p50": _pct([r["ms_first"] for r in rows], 0.5),
                       "first_p95": _pct([r["ms_first"] for r in rows], 0.95)},
        "per_lang": {},
    }
    if "rule_confident" in (rows[0] if rows else {}):
        unsure = [r for r in rows if not r["rule_confident"]]
        out["rule_unsure"] = len(unsure)
        out["accuracy_on_rule_unsure"] = {"rule": _acc(unsure, "rule"), "typed": _acc(unsure, "typed"),
                                          "combined": _acc(unsure, "combined")}
    for lang in sorted({r.get("lang") for r in rows if r.get("lang")}):
        sub = [r for r in rows if r.get("lang") == lang]
        out["per_lang"][lang] = {"n": len(sub), "rule": _acc(sub, "rule"), "typed": _acc(sub, "typed"),
                                 "combined": _acc(sub, "combined")}
    if cache:
        out["latency_ms"]["second_p50"] = _pct([r["ms_second"] for r in rows], 0.5)
        out["latency_ms"]["second_p95"] = _pct([r["ms_second"] for r in rows], 0.95)
        c1 = [r["cached_first"] for r in rows if r["cached_first"] is not None]
        c2 = [r["cached_second"] for r in rows if r["cached_second"] is not None]
        out["cached_tokens_mean"] = {
            "first": round(sum(c1) / len(c1), 1) if c1 else None,
            "second": round(sum(c2) / len(c2), 1) if c2 else None,
        }
        oks = [r["lang_ok"] for r in rows if r["lang_ok"] is not None]
        out["sanity_language_field_accuracy"] = round(sum(oks) / len(oks), 4) if oks else None
    return out


def to_markdown(report: Dict[str, Any]) -> str:
    lines = [f"Endpoint: `{report['wire']}` · model `{report['model']}` · thresholds: "
             f"confidence >= {report['min_confidence']}, mass >= {report['min_mass']}", ""]
    lines.append("| task | n | rule | typed (raw) | combined | flips (right) | typed when accepted | p50 ms | p95 ms |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for name, s in report["tasks"].items():
        acc = s["accuracy"]
        fmt = lambda v: "—" if v is None else f"{v:.0%}"  # noqa: E731
        lines.append(f"| {name} | {s['n']} | {fmt(acc['rule'])} | {fmt(acc['typed'])} | {fmt(acc['combined'])} | "
                     f"{s['flips']} ({s['flips_right']}) | {fmt(s['typed_when_accepted'])} | "
                     f"{s['latency_ms']['first_p50']} | {s['latency_ms']['first_p95']} |")
    fresh = report["tasks"].get("freshness")
    if fresh and "second_p50" in fresh["latency_ms"]:
        lines += ["", f"Shared prefix: first field p50 {fresh['latency_ms']['first_p50']} ms, "
                      f"second field on the same context p50 {fresh['latency_ms']['second_p50']} ms; "
                      f"cached prompt tokens (mean) first {fresh['cached_tokens_mean']['first']}, "
                      f"second {fresh['cached_tokens_mean']['second']}."]
    for name, s in report["tasks"].items():
        if s["calibration"]:
            lines += ["", f"Calibration — {name}:", "", "| confidence | n | mean conf. | accuracy |", "|---|---|---|---|"]
            for b in s["calibration"]:
                lines.append(f"| {b['bucket']} | {b['n']} | {b['mean_confidence']} | {b['accuracy']:.0%} |")
    return "\n".join(lines)


def print_summary(report: Dict[str, Any]) -> None:
    print(f"typed decision eval - {report['wire']} {report['url']} model={report['model']}")
    for name, s in report["tasks"].items():
        acc = s["accuracy"]
        print(f"  {name}: n={s['n']}  rule={acc['rule']}  typed={acc['typed']}  combined={acc['combined']}"
              f"  flips={s['flips']} (right {s['flips_right']})  methods={s['methods']}")
        if "rule_unsure" in s:
            print(f"    rule unsure on {s['rule_unsure']}: {s['accuracy_on_rule_unsure']}")
        print(f"    latency ms: {s['latency_ms']}")
        if "cached_tokens_mean" in s:
            print(f"    cached tokens (mean): {s['cached_tokens_mean']}  "
                  f"sanity language field: {s['sanity_language_field_accuracy']}")
        for b in s["calibration"]:
            print(f"    conf {b['bucket']}: n={b['n']} acc={b['accuracy']}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _resolve_runner(args) -> Tuple[Optional[Runner], str]:
    timeout_s = max(0.2, args.timeout_ms / 1000.0)
    if args.fake:
        import httpx
        td._TRANSPORT = httpx.MockTransport(_FakeModel())
        return Runner(FAKE_URL, "fake-model", "openai", {}, timeout_s), ""
    if args.url:
        wire, url = td.wire_for(args.url)
        if wire == "unsupported":
            return None, f"unsupported endpoint: {args.url}"
        headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
        if not args.allow_load:
            reason = td.residency_reason(args.url, args.model)
            if reason:
                return None, (f"{reason}: {args.model} is not loaded at {args.url} "
                              "(load it yourself, or pass --allow-load)")
        return Runner(url, args.model, wire, headers, timeout_s), ""
    prep = td._prepare("utility", args.owner or None)
    if prep.get("reason") and not (args.allow_load and prep["reason"] in
                                   ("model_not_resident", "residency_unknown", "model_busy")):
        return None, f"configured utility endpoint unavailable: {prep['reason']}"
    if prep.get("reason"):
        url, model, headers = td._resolve("utility", args.owner or None)
        wire, url = td.wire_for(url)
        return Runner(url, model, wire, headers, timeout_s), ""
    return Runner(prep["url"], prep["model"], prep["wire"], prep["headers"], timeout_s), ""


async def run_eval(args) -> Dict[str, Any]:
    runner, error = _resolve_runner(args)
    if runner is None:
        return {"error": error}
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    min_conf = td.default_min_confidence() if args.min_confidence is None else args.min_confidence
    min_mass = td.default_min_mass() if args.min_mass is None else args.min_mass
    tasks: Dict[str, Any] = {}
    rows: Dict[str, Any] = {}
    wanted = set(args.task or ["freshness", "entity_types"])
    if "freshness" in wanted:
        fr = await run_freshness(runner, cases["freshness"][: args.limit or None], min_conf, min_mass)
        tasks["freshness"], rows["freshness"] = summarize(fr, cache=True), fr
    if "entity_types" in wanted:
        et = await run_entity_types(runner, cases["entity_types"][: args.limit or None], min_conf, min_mass)
        tasks["entity_types"], rows["entity_types"] = summarize(et, cache=False), et
    return {"url": runner.url, "model": runner.model, "wire": runner.wire,
            "min_confidence": min_conf, "min_mass": min_mass, "tasks": tasks, "rows": rows}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate typed decisions against labelled cases.")
    parser.add_argument("--url", help="Endpoint (OpenAI-compatible base or chat URL, or an Ollama root).")
    parser.add_argument("--model", help="Model name at --url.")
    parser.add_argument("--api-key", default=os.environ.get("TYPED_DECISION_EVAL_KEY", ""))
    parser.add_argument("--owner", default="", help="Owner whose Utility setting to resolve (no --url).")
    parser.add_argument("--fake", action="store_true", help="Offline deterministic fake model (CI).")
    parser.add_argument("--allow-load", action="store_true", help="Call the model even if it is not resident.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--task", action="append", choices=["freshness", "entity_types"])
    parser.add_argument("--limit", type=int, default=0, help="Only the first N cases per task.")
    parser.add_argument("--timeout-ms", type=int, default=15000, help="Per-request timeout.")
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument("--min-mass", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)
    if args.url and not args.model:
        parser.error("--url needs --model")
    report = asyncio.run(run_eval(args))
    if report.get("error"):
        print(report["error"], file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    elif args.markdown:
        print(to_markdown(report))
    else:
        print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
