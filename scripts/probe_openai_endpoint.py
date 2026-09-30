"""Probe an OpenAI-compatible server and print what it does, not what it claims.

    python scripts/probe_openai_endpoint.py --base-url http://127.0.0.1:8081/v1 --model <name>
    python scripts/probe_openai_endpoint.py --base-url ... --model ... --probe vision --json

Each capability is reported as ``supported`` (the server did it), ``unsupported``
(it said it does not, or answered in a way that shows it does not) or
``unknown`` (nothing was learned: timeout, bad key, route not found). Nothing is
stored; the key, if any, is read from ``--api-key`` or the ``PROBE_API_KEY``
environment variable and is only sent to the server being probed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main(argv=None) -> int:
    from src import model_calibration as mcal, openai_probes as probes

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="server address, usually ending in /v1")
    parser.add_argument("--model", required=True, help="model name as the server lists it")
    parser.add_argument("--api-key", default=os.environ.get("PROBE_API_KEY", ""))
    parser.add_argument("--probe", action="append", choices=list(probes.SUPPORTED_PROBES),
                        help="probe to run (repeatable); default: tool_calling, streaming_tool_calls, json_mode")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args(argv)
    try:
        result = probes.probe_url(args.base_url, args.model, api_key=args.api_key, include=args.probe)
    except probes.ProbeRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"{result['model']} @ {args.base_url}  [{result['protocol']}]")
        for key in mcal.TEST_KEYS:
            tested = result["manifest"]["tested"].get(key)
            if tested is None:
                continue
            evidence = tested.get("evidence") or {}
            state = mcal.probe_state(tested)
            why = evidence.get("reason") or ""
            status = f" (HTTP {evidence['status']})" if evidence.get("status") else ""
            print(f"  {key:26s} {state:12s}{status} {why}")
    states = {mcal.probe_state(v) for v in result["manifest"]["tested"].values()}
    return 0 if "unknown" not in states else 1


if __name__ == "__main__":
    raise SystemExit(main())
