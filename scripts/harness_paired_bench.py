#!/usr/bin/env python
"""Paired harness bench: the same model, the same tasks, two arms (H24).

An arm is one of:

    current                 this checkout, default settings
    rev:<git revision>      that revision, checked out into a throwaway worktree
    set:key=value[,k=v...]  this checkout with those settings written to the run's own data dir

Every (arm, case, repeat) runs against its own server process, data dir,
workspace and canary directory; nothing is shared between runs. Each run is
scored on the case's checkable outcome (files, tests, effect counters), on the
boundaries it kept (nothing outside the allowed files changed, the canary did
not move, no effect ran twice) and on rounds, seconds and tokens.

Without --endpoint the model is scripted (no GPU, deterministic, good for
checking the bench itself). With --endpoint the real model answers.

Examples (Windows, from the repository root, against the local 27B):

    .\\venv\\Scripts\\python.exe scripts\\harness_paired_bench.py ^
        --endpoint http://127.0.0.1:8081/v1 --effort medium --repeats 3 ^
        --baseline set:llm_projection_mode=legacy --candidate current

    .\\venv\\Scripts\\python.exe scripts\\harness_paired_bench.py --plan ^
        --baseline rev:HEAD~1 --candidate current

Exit status: 0 verdict faster/equivalent, 1 regression/blocked/slower,
2 inconclusive, 3 the bench itself could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

EXIT_BY_VERDICT = {"faster": 0, "equivalent": 0, "slower": 1, "regression": 1, "blocked": 1, "inconclusive": 2}


# -- arms ----------------------------------------------------------------------

class Arm:
    def __init__(self, name: str, spec: str, repo: Path, settings: Dict[str, Any], revision: str) -> None:
        self.name, self.spec, self.repo, self.settings, self.revision = name, spec, repo, settings, revision

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "spec": self.spec, "revision": self.revision, "settings": self.settings}


def _coerce(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def parse_settings(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        if "=" not in part:
            raise SystemExit(f"bad setting {part!r}: expected key=value")
        key, _, value = part.partition("=")
        out[key.strip()] = _coerce(value.strip())
    return out


def _git(*args: str, cwd: Optional[Path] = None) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd or REPO), capture_output=True, text=True,
                          check=True).stdout.strip()


def head_revision(repo: Optional[Path] = None) -> str:
    repo = repo or REPO
    try:
        rev = _git("rev-parse", "--short", "HEAD", cwd=repo)
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no", cwd=repo))
        return rev + ("+uncommitted" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def make_arm(role: str, spec: str, workdir: Path) -> Arm:
    spec = spec.strip()
    if spec == "current":
        return Arm(role, spec, REPO, {}, head_revision())
    if spec.startswith("set:"):
        return Arm(role, spec, REPO, parse_settings(spec[4:]), head_revision())
    if spec.startswith("rev:"):
        rev = spec[4:].strip()
        target = workdir / f"rev-{role}"
        try:
            _git("worktree", "add", "--detach", str(target), rev)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"cannot check out {rev!r}: {exc.stderr.strip() if exc.stderr else exc}")
        return Arm(role, spec, target, {}, _git("rev-parse", "--short", "HEAD", cwd=target))
    raise SystemExit(f"bad arm {spec!r}: use current, rev:<git revision> or set:key=value[,...]")


def drop_worktrees(arms: List[Arm]) -> None:
    for arm in arms:
        if arm.repo != REPO:
            subprocess.run(["git", "worktree", "remove", "--force", str(arm.repo)], cwd=str(REPO),
                           capture_output=True, text=True)


# -- model ---------------------------------------------------------------------

def detect_model(endpoint: str) -> str:
    with urllib.request.urlopen(endpoint.rstrip("/") + "/models", timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    rows = data.get("data") or data.get("models") or []
    if not rows:
        raise SystemExit(f"{endpoint}/models lists no model; pass --model")
    first = rows[0]
    return str(first.get("id") or first.get("name") or first)


# -- run -----------------------------------------------------------------------

def plan(cases: List[str], arms: List[Arm], repeats: int) -> List[Tuple[str, str, int]]:
    """Run order: per repeat and case, baseline then candidate, so drift in the
    model or the machine hits both arms alike."""
    out: List[Tuple[str, str, int]] = []
    for repeat in range(repeats):
        for case in cases:
            for arm in arms:
                out.append((arm.name, case, repeat))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", help="OpenAI-compatible base URL of the real model, e.g. http://127.0.0.1:8081/v1; "
                                       "omit to use the scripted model")
    ap.add_argument("--model", help="model id to ask for (default: the first one the endpoint lists)")
    ap.add_argument("--effort", help="reasoning effort sent with every turn (low/medium/high); same for both arms")
    ap.add_argument("--baseline", default="set:llm_projection_mode=legacy", help="baseline arm (default %(default)s)")
    ap.add_argument("--candidate", default="current", help="candidate arm (default %(default)s)")
    ap.add_argument("--cases", help="comma-separated case names (default: all)")
    ap.add_argument("--repeats", type=int, default=3, help="runs per case and arm (default 3)")
    ap.add_argument("--min-repeats", type=int, default=3, help="fewer runs than this per case is inconclusive")
    ap.add_argument("--timeout", type=float, default=None, help="seconds one turn may take (default 120 scripted, 900 real)")
    ap.add_argument("--out", help="report path (default: DATA_DIR/benchmarks/harness_pair/<time>.json)")
    ap.add_argument("--plan", action="store_true", help="print what would run and stop")
    ap.add_argument("--list-cases", action="store_true")
    args = ap.parse_args(argv)

    from src.bench import harness_pair as hp
    from tests.eval import paired_cases as pc

    if args.list_cases:
        print("\n".join(pc.case_names()))
        return 0
    cases = [c.strip() for c in args.cases.split(",")] if args.cases else pc.case_names()
    unknown = [c for c in cases if c not in pc.case_names()]
    if unknown:
        print(f"unknown case(s) {unknown}; known: {pc.case_names()}", file=sys.stderr)
        return 3

    live = bool(args.endpoint)
    model: Optional[str] = None
    if live:
        try:
            model = args.model or detect_model(args.endpoint)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot reach {args.endpoint}: {exc}", file=sys.stderr)
            return 3
    timeout = args.timeout or (900.0 if live else 120.0)

    workdir = Path(tempfile.mkdtemp(prefix="faustus-pair-arms-"))
    arms: List[Arm] = []
    try:
        arms = [make_arm("baseline", args.baseline, workdir), make_arm("candidate", args.candidate, workdir)]
        order = plan(cases, arms, args.repeats)
        conditions: Dict[str, Any] = {
            "mode": "real model" if live else "scripted model",
            "endpoint": args.endpoint or "scripted", "model": model or "fake-coder", "effort": args.effort or "default",
            "same_model": True, "repeats": args.repeats, "cases": cases,
            "arms": {a.name: a.describe() for a in arms},
            "python": sys.version.split()[0], "platform": platform.platform(),
            "note": ("The scripted model replays fixed answers: it checks the harness and its boundaries, "
                     "not model quality." if not live else
                     "Real model: sampling noise is real. Read seconds and tokens only with enough repeats."),
        }
        if args.plan:
            print(json.dumps({"conditions": conditions, "runs": len(order),
                              "order": [{"arm": a, "case": c, "repeat": r} for a, c, r in order]}, indent=1))
            return 0

        by_name = {a.name: a for a in arms}
        records: Dict[str, List[hp.RunRecord]] = {a.name: [] for a in arms}
        for i, (arm_name, case, repeat) in enumerate(order, 1):
            arm = by_name[arm_name]
            print(f"[{i}/{len(order)}] {arm_name:9} {case} #{repeat + 1}", flush=True)
            rec = pc.run_case(case, arm=arm_name, repeat=repeat, repo=str(arm.repo) if arm.repo != REPO else None,
                              settings=arm.settings or None, endpoint=args.endpoint, model=model,
                              effort=args.effort, timeout=timeout)
            records[arm_name].append(rec)
            print(f"      {'ok ' if rec.success else 'FAIL'} {rec.seconds}s rounds={rec.rounds} "
                  f"violations={len(rec.violations)} {rec.detail[:120]}", flush=True)

        report = hp.build_report(conditions, records, baseline="baseline", candidate="candidate",
                                 min_repeats=args.min_repeats)
        out = args.out or os.path.join(hp.reports_dir(), time.strftime("%Y%m%d-%H%M%S") + ".json")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
        print()
        print(hp.render_text(report))
        print(f"\nreport: {out}")
        return EXIT_BY_VERDICT.get(report["comparison"]["verdict"], 3)
    finally:
        drop_worktrees(arms)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
