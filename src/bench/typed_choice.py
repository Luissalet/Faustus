"""src/bench/typed_choice.py — CLI benchmark for typed choice decisions.

Runs a small, fixed set of routing / yes-no / classification questions with
known answers through both ``src.typed_choice.typed_choice`` (one
constrained forward pass, logprob-scored) and
``src.typed_choice.generated_choice`` (a normal short generation, text
parsed) against the same local model, and reports accuracy, mean latency and
how often the two methods agree.

    python -m src.bench.typed_choice --url http://127.0.0.1:8082 --model X
    python -m src.bench.typed_choice --url http://127.0.0.1:8082 --model X --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any, Dict, List, Optional

from src.typed_choice import generated_choice, typed_choice

# Each fixture: (question, options, context, expected_option). Short,
# unambiguous routing/yes-no/classification questions any reasonably capable
# local model should get right — the point is measuring method quality/speed,
# not model quality.
FIXTURES: List[Dict[str, Any]] = [
    {
        "question": "Is Paris the capital of France?",
        "options": ["yes", "no"],
        "context": "",
        "expected": "yes",
    },
    {
        "question": "Is the sky green?",
        "options": ["yes", "no"],
        "context": "",
        "expected": "no",
    },
    {
        "question": "Which category does this message belong to?",
        "options": ["billing", "technical support", "sales"],
        "context": "Message: \"My credit card was charged twice for the same order.\"",
        "expected": "billing",
    },
    {
        "question": "Which category does this message belong to?",
        "options": ["billing", "technical support", "sales"],
        "context": "Message: \"The app crashes every time I open the settings page.\"",
        "expected": "technical support",
    },
    {
        "question": "Which category does this message belong to?",
        "options": ["billing", "technical support", "sales"],
        "context": "Message: \"Can I get a demo of the enterprise plan?\"",
        "expected": "sales",
    },
    {
        "question": "Is 17 a prime number?",
        "options": ["yes", "no"],
        "context": "",
        "expected": "yes",
    },
    {
        "question": "Is 21 a prime number?",
        "options": ["yes", "no"],
        "context": "",
        "expected": "no",
    },
    {
        "question": "What is the sentiment of this review?",
        "options": ["positive", "negative", "neutral"],
        "context": "Review: \"Absolutely love this product, works perfectly every time!\"",
        "expected": "positive",
    },
    {
        "question": "What is the sentiment of this review?",
        "options": ["positive", "negative", "neutral"],
        "context": "Review: \"Broke after two days, complete waste of money.\"",
        "expected": "negative",
    },
    {
        "question": "Which tool should handle this request?",
        "options": ["search the web", "run a shell command", "read a local file"],
        "context": "Request: \"What is today's weather in Madrid?\"",
        "expected": "search the web",
    },
    {
        "question": "Which tool should handle this request?",
        "options": ["search the web", "run a shell command", "read a local file"],
        "context": "Request: \"List the files in the current directory.\"",
        "expected": "run a shell command",
    },
    {
        "question": "Does this sentence contain a date?",
        "options": ["yes", "no"],
        "context": "Sentence: \"The meeting is scheduled for March 3rd.\"",
        "expected": "yes",
    },
]


async def _run_one(fn, fixture: Dict[str, Any], *, url: str, model: str) -> Dict[str, Any]:
    t0 = time.monotonic()
    result = await fn(
        fixture["question"], fixture["options"], context=fixture.get("context", ""),
        url=url, model=model,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000
    correct = bool(result.get("choice") == fixture["expected"]) and not result.get("error")
    return {"result": result, "elapsed_ms": elapsed_ms, "correct": correct}


async def run_benchmark(url: str, model: str) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for fixture in FIXTURES:
        typed = await _run_one(typed_choice, fixture, url=url, model=model)
        generated = await _run_one(generated_choice, fixture, url=url, model=model)
        agree = (typed["result"].get("choice") == generated["result"].get("choice")
                  and not typed["result"].get("error") and not generated["result"].get("error"))
        rows.append({
            "question": fixture["question"], "expected": fixture["expected"],
            "typed_choice": typed, "generated_choice": generated, "agree": agree,
        })

    def _summary(key: str) -> Dict[str, Any]:
        n = len(rows)
        correct = sum(1 for r in rows if r[key]["correct"])
        latencies = [r[key]["elapsed_ms"] for r in rows]
        errors = sum(1 for r in rows if r[key]["result"].get("error"))
        return {
            "accuracy": round(correct / n, 4) if n else 0.0,
            "correct": correct, "total": n, "errors": errors,
            "mean_latency_ms": round(sum(latencies) / n, 2) if n else 0.0,
        }

    agreement = sum(1 for r in rows if r["agree"]) / len(rows) if rows else 0.0
    return {
        "url": url, "model": model,
        "typed_choice": _summary("typed_choice"),
        "generated_choice": _summary("generated_choice"),
        "agreement_rate": round(agreement, 4),
        "rows": rows,
    }


def _print_report(report: Dict[str, Any]) -> None:
    tc, gc = report["typed_choice"], report["generated_choice"]
    print(f"typed_choice benchmark - url={report['url']} model={report['model']}")
    print(f"  cases: {tc['total']}")
    print(f"  typed_choice:     accuracy {tc['accuracy']:.0%}  "
          f"mean latency {tc['mean_latency_ms']:.1f} ms  errors {tc['errors']}")
    print(f"  generated_choice: accuracy {gc['accuracy']:.0%}  "
          f"mean latency {gc['mean_latency_ms']:.1f} ms  errors {gc['errors']}")
    print(f"  agreement rate:   {report['agreement_rate']:.0%}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark typed choice decisions vs. free-text generation.")
    parser.add_argument("--url", required=True, help="Base URL of an OpenAI-compatible local server (e.g. a llama.cpp server).")
    parser.add_argument("--model", required=True, help="Model name to use.")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON instead of a summary.")
    args = parser.parse_args(argv)

    report = asyncio.run(run_benchmark(args.url, args.model))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
