"""Opt-in 2k/8k Ollama comparison through Faustus's existing benchmark runner.

python -m scripts.benchmark_context_pair --endpoint http://127.0.0.1:11434/v1 \
    --model MODEL --out data/benchmarks/context-pair.json [--run]
Without --run, produces a plan only: no network or model calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from urllib.parse import urlparse

from core.atomic_io import atomic_write_json

CONTEXTS = (2048, 8192)


def make_plan(endpoint: str, model: str) -> dict:
    url = urlparse(endpoint)
    if (url.scheme not in {"http", "https"}
            or url.hostname not in {"localhost", "127.0.0.1"}
            or url.port != 11434 or url.username or url.password
            or url.query or url.fragment or url.path.rstrip("/") not in {"", "/v1"}):
        raise ValueError("Use a local Ollama endpoint on port 11434, optionally ending in /v1")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("An explicit model is required")
    return {
        "schema_version": 1, "state": "planned", "endpoint": endpoint.rstrip("/"),
        "model": model.strip(), "requested_contexts": list(CONTEXTS),
        "suite": "es_conversation", "budget": {"max_cases": 3, "max_seconds": 180},
        "context_meaning": "requested capacity, not measured prompt length",
        "runs": [], "heuristics": [],
    }


def _resident(endpoint: str, model: str) -> dict:
    """Read metadata only; never load, pull or stop a model."""
    import httpx
    url = urlparse(endpoint)
    root = f"{url.scheme}://{url.hostname}:{url.port}"
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        response = client.get(root + "/api/ps")
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Ollama /api/ps did not return a models list")
    for row in rows:
        if isinstance(row, dict) and model in {row.get("name"), row.get("model")}:
            # Unknown context stays unknown; never equate request with observation.
            context = row.get("context_length")
            return {"model": model, "context_length": context
                    if type(context) is int and context > 0 else None}
    raise ValueError("Requested model is not resident; benchmark will not load it")


async def execute(plan: dict, out: str) -> dict:
    from src.bench import profiles, runner

    endpoint, model = plan["endpoint"], plan["model"]
    report = dict(plan, state="running", runs=[], heuristics=[])
    try:
        for context in CONTEXTS:
            before = _resident(endpoint, model)
            base = profiles.current_profile(endpoint, model, "context-benchmark")
            if base.engine.implementation != "ollama":
                raise ValueError("Configured engine identity is not Ollama")
            profile = replace(base, id=f"{base.id}-ctx{context}",
                              options={**base.options, "num_ctx": context}, fingerprint="")
            # Reparse to compute the fingerprint for the changed options.
            raw_profile = profile.to_dict()
            raw_profile.pop("fingerprint")
            profile = type(profile).parse(raw_profile)
            planned = runner.plan(profile, plan["suite"], plan["budget"],
                                  "context-benchmark", endpoint_url=endpoint)
            report["heuristics"].append({"requested_context": context,
                                         "estimate_seconds": planned.summary.estimate_seconds})
            finished = await runner.start(planned.id)
            data = finished.to_dict()
            data["summary"].pop("estimate_seconds", None)
            row = {"requested_context": context, "resident_before": before, "run": data}
            report["runs"].append(row)
            atomic_write_json(out, report, indent=2)
            if finished.state != "completed" or any(s.error for s in finished.samples):
                raise ValueError("Benchmark did not complete cleanly; inspect recorded samples")
            row["resident_after"] = _resident(endpoint, model)
            row["context_verified"] = row["resident_after"]["context_length"] == context
        report["state"] = "completed"
    except Exception as exc:
        report["state"] = "blocked_or_failed"
        report["error"] = str(exc)
    report["contexts_verified"] = (len(report["runs"]) == len(CONTEXTS)
                                   and all(row.get("context_verified") for row in report["runs"]))
    atomic_write_json(out, report, indent=2)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = make_plan(args.endpoint, args.model)
    except ValueError as exc:
        parser.error(str(exc))
    if args.run:
        report = asyncio.run(execute(plan, args.out))
    else:
        report = plan
        atomic_write_json(args.out, report, indent=2)
    print(json.dumps({"state": report["state"], "report": args.out}))
    return 0 if report["state"] in {"planned", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
