#!/usr/bin/env python3
"""
bench_context_engine.py — the legacy context blocks against the compiled packet, offline.

Before `agent_context_engine` is switched on for real, one question has to be
answered with numbers rather than with a feeling: for the questions this
install actually gets, does the compiled packet carry what the legacy prompt
carried, at what size, and at what latency?  This script answers it for any
data dir, with no model and no network:

* **legacy** — what the agent prompt injects today with the engine off: the
  learned-memory block (`memory_engine.pack_detail`, the same packer the agent
  loop uses) and the saved-memory preface (`ChatProcessor.build_context_preface`
  with memory on and web/RAG off: the personal-document RAG needs an embedding
  model and is left out on both sides).
* **packet** — `ContextCompiler.shadow()` for the same question, owner and
  project: a real compile that records no ledger row, rendered exactly the way
  `wiring.deliver_round` renders it for the model.

Per query it reports tokens (one estimator for both sides), sections, sources,
compile latency, and the recall of `expect_refs` (source_ref prefixes); then
p50/p95 latency and mean recall over the whole set.  Budget omissions the
packet can still bring back with `context_recall` are counted separately as
"recoverable".

Usage::

    # the committed baseline: a throwaway data dir with invented content
    python scripts/bench_context_engine.py --demo --write

    # any data dir (reads it; the compile may create the engine's own
    # context_engine.db there — pass --snapshot to work on a temporary copy)
    python scripts/bench_context_engine.py --data-dir /path/to/data \\
        --queries docs/evals/context_engine_queries.json --owner someone

Query file: a JSON list of ``{"query": str, "owner"?: str, "project_id"?: str,
"workspace"?: str, "expect_refs"?: [source_ref prefixes]}``.
The written report never contains the data dir path, only ``--label``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_QUERIES = os.path.join(ROOT, "docs", "evals", "context_engine_queries.json")
DEFAULT_REPORT = os.path.join(ROOT, "docs", "evals", "context-engine-baseline.md")
DEMO_OWNER = "ada"


# ── arguments ──────────────────────────────────────────────────────────────

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="", help="data dir to measure (default: "
                        "ODYSSEUS_DATA_DIR or the app default)")
    parser.add_argument("--demo", action="store_true",
                        help="seed a throwaway data dir with invented content and use it")
    parser.add_argument("--snapshot", action="store_true",
                        help="copy --data-dir to a temporary dir first (zero writes to it)")
    parser.add_argument("--queries", default=DEFAULT_QUERIES, help="query JSON file")
    parser.add_argument("--owner", default="", help="default owner for queries without one")
    parser.add_argument("--model", default="bench-model", help="model name the packet is budgeted for")
    parser.add_argument("--context-length", type=int, default=32768)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--packet-budget", type=int, default=0,
                        help="token budget for the packet (0 = let the compiler size it; "
                        "the compiler never goes below its 512-token floor)")
    parser.add_argument("--runs", type=int, default=3, help="compiles per query, for latency")
    parser.add_argument("--label", default="", help="data-set label printed in the report")
    parser.add_argument("--write", nargs="?", const=DEFAULT_REPORT, default="",
                        help=f"write the markdown report (default path: {os.path.relpath(DEFAULT_REPORT, ROOT)})")
    parser.add_argument("--json", action="store_true", help="print raw JSON rows instead of markdown")
    return parser.parse_args(argv)


# ── the demo data set (invented content only) ──────────────────────────────

DEMO_PERSONAL = [
    {"id": "demo-name", "text": "My name is Ada and I work at Cordera Labs.",
     "category": "identity", "pinned": True},
    {"id": "demo-deploy", "text": "Bluehaven deploys go out on Thursdays after the "
     "staging smoke test passes.", "category": "fact"},
    {"id": "demo-coffee", "text": "Prefers oat milk in coffee.", "category": "preference"},
    {"id": "demo-villanueva", "text": "Villanueva is the client contact for the "
     "Bluehaven migration; send weekly status on Mondays.", "category": "contact"},
    {"id": "demo-editor", "text": "Uses tabs of width 4 in Python files.", "category": "preference"},
]

DEMO_RULES = [
    ("Always run the Bluehaven test suite with `make test` before claiming a fix.",
     "procedure", "testing"),
    ("Never force-push to the release branch of Bluehaven; open a revert instead.",
     "anti_pattern", "git"),
    ("Database migrations in Bluehaven must be reversible and ship with a down step.",
     "decision", "database"),
    ("Status reports for Villanueva use three bullets: done, next, blocked.",
     "preference", "writing"),
    ("The Cordera Labs staging cluster lives in the eu-west region.", "fact", "infra"),
]


def seed_demo(data_dir: str) -> None:
    """Invented memories in a fresh data dir: memory.json with fixed ids (so
    `expect_refs` can name them) and learned rules in the memory engine."""
    os.makedirs(data_dir, exist_ok=True)
    stamp = 1767225600  # 2026-01-01, fixed so the report is reproducible
    entries = [dict(e, owner=DEMO_OWNER, timestamp=stamp, source="demo", uses=0)
               for e in DEMO_PERSONAL]
    with open(os.path.join(data_dir, "memory.json"), "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
    from src import memory_engine

    for text, kind, category in DEMO_RULES:
        try:
            memory_engine.add_item(text, owner=DEMO_OWNER, project="", level="procedural",
                                   category=category, type=kind,
                                   trust_class="human_explicit", status="active",
                                   maturity="established")
        except Exception as exc:  # noqa: BLE001 - a demo row that fails is skipped
            print(f"[demo] skipped a rule: {exc}", file=sys.stderr)


# ── measuring ──────────────────────────────────────────────────────────────

def percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[rank]


def legacy_blocks(query: str, *, owner: str, project: str) -> Dict[str, Any]:
    """The legacy memory/context blocks and the refs they carry."""
    from src.prompt_security import untrusted_context_message

    messages: List[Dict[str, Any]] = []
    refs: List[str] = []
    started = time.perf_counter()
    try:
        from src import memory_engine

        if memory_engine.injection_enabled() and memory_engine.injection_budget() > 0:
            detail = memory_engine.pack_detail(owner, project, query,
                                               memory_engine.injection_budget())
            if detail.get("text"):
                messages.append(untrusted_context_message("learned memory", detail["text"]))
                refs += [f"mem:{i}" for i in detail.get("ids") or []]
    except Exception as exc:  # noqa: BLE001 - one missing block is a measurement
        print(f"[legacy] learned memory failed: {exc}", file=sys.stderr)
    try:
        from src.chat_processor import ChatProcessor
        from src.constants import DATA_DIR
        from src.memory import MemoryManager

        manager = MemoryManager(DATA_DIR)
        processor = ChatProcessor(manager, None)
        preface, _, _ = processor.build_context_preface(
            message=query, session=None, use_web=False, use_rag=False, use_memory=True,
            owner=owner or None, use_skills=False)
        saved = [m for m in preface
                 if str((m.get("metadata") or {}).get("source") or "").startswith("saved memory")]
        messages += saved
        used = {str(m.get("text") or "") for m in getattr(processor, "_last_used_memories", [])}
        for entry in manager.load(owner=owner or None):
            if str(entry.get("text") or "") in used and entry.get("id"):
                refs.append(f"pmem:{entry['id']}")
    except Exception as exc:  # noqa: BLE001
        print(f"[legacy] saved memory failed: {exc}", file=sys.stderr)
    return {"messages": messages, "refs": refs,
            "ms": (time.perf_counter() - started) * 1000.0}


async def compile_packet(query: str, *, owner: str, project: str, workspace: str,
                         args: argparse.Namespace, legacy_messages: List[Dict[str, Any]]
                         ) -> Dict[str, Any]:
    """One shadow compile (no ledger row), rendered as the live path renders it."""
    from dataclasses import replace

    from src.context_engine import wiring
    from src.context_engine.compiler import compiler
    from src.context_engine.contracts import ContextPacket

    user = {"role": "user", "content": query}
    request = wiring.build_request(owner=owner, session_id="", model=args.model,
                                   workspace=workspace, project_id=project,
                                   messages=[user], agent_mode=True)
    if args.packet_budget > 0:
        request = replace(request, policy=replace(request.policy,
                                                  token_budget=int(args.packet_budget)))
    started = time.perf_counter()
    report = await compiler().shadow(
        request, messages=list(legacy_messages) + [user],
        context_length=int(args.context_length), window_known=True,
        max_output_tokens=int(args.max_output_tokens))
    elapsed = (time.perf_counter() - started) * 1000.0
    if report.get("error") or not isinstance(report.get("packet"), dict):
        return {"ms": elapsed, "error": str(report.get("error") or "no packet")}
    packet = ContextPacket.parse(report["packet"])
    return {"ms": elapsed, "packet": packet, "rendered": wiring._render_live(packet)}


def _hit(prefixes: Sequence[str], refs: Sequence[str]) -> Optional[float]:
    wanted = [str(p) for p in prefixes or () if str(p)]
    if not wanted:
        return None
    return sum(1 for p in wanted if any(str(r).startswith(p) for r in refs)) / len(wanted)


async def measure(queries: List[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    from src.context_engine.budgets import estimator_for

    estimator = estimator_for(args.model)
    rows: List[Dict[str, Any]] = []
    for index, spec in enumerate(queries, 1):
        query = str(spec.get("query") or "").strip()
        if not query:
            continue
        owner = str(spec.get("owner") or args.owner or "")
        project = str(spec.get("project_id") or "")
        workspace = str(spec.get("workspace") or "")
        legacy_runs = [legacy_blocks(query, owner=owner, project=workspace or project)
                       for _ in range(max(1, args.runs))]
        legacy = legacy_runs[-1]
        compiles = [await compile_packet(query, owner=owner, project=project,
                                         workspace=workspace, args=args,
                                         legacy_messages=legacy["messages"])
                    for _ in range(max(1, args.runs))]
        last = compiles[-1]
        row: Dict[str, Any] = {
            "n": index, "query": query, "owner": owner, "project_id": project,
            "legacy_tokens": estimator.count_messages(legacy["messages"]) if legacy["messages"] else 0,
            "legacy_blocks": len(legacy["messages"]),
            "legacy_refs": legacy["refs"],
            "legacy_ms": [r["ms"] for r in legacy_runs],
            "compile_ms": [c["ms"] for c in compiles],
            "error": last.get("error", ""),
        }
        packet = last.get("packet")
        if packet is not None:
            items = [item for section in packet.sections
                     if section.kind != "recent_messages" for item in section.items]
            refs = [item.source_ref for item in items]
            recoverable = [o.source_ref for o in packet.omissions if o.reason == "budget"]
            by_type: Dict[str, int] = {}
            for item in items:
                by_type[item.source_type] = by_type.get(item.source_type, 0) + 1
            row.update({
                "packet_tokens": (estimator.count_messages(
                    [{"role": "user", "content": last["rendered"]}]) if last["rendered"] else 0),
                "sections": sorted({s.kind for s in packet.sections
                                    if s.items and s.kind != "recent_messages"}),
                "packet_sources": by_type,
                "packet_refs": refs,
                "omitted": len(packet.omissions),
                "recoverable": recoverable,
                "degraded": bool(packet.degraded),
            })
        else:
            row.update({"packet_tokens": 0, "sections": [], "packet_sources": {},
                        "packet_refs": [], "omitted": 0, "recoverable": [], "degraded": True})
        expect = spec.get("expect_refs") or []
        row["recall_legacy"] = _hit(expect, row["legacy_refs"])
        row["recall_packet"] = _hit(expect, row["packet_refs"])
        row["recall_packet_recoverable"] = _hit(expect, list(row["packet_refs"]) + list(row["recoverable"]))
        rows.append(row)
    return rows


# ── reporting ──────────────────────────────────────────────────────────────

def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _cell(text: str, limit: int = 48) -> str:
    clean = " ".join(str(text).split()).replace("|", "/")
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _mean(values: Sequence[Optional[float]]) -> Optional[float]:
    kept = [v for v in values if v is not None]
    return (sum(kept) / len(kept)) if kept else None


def render_markdown(rows: List[Dict[str, Any]], args: argparse.Namespace, label: str) -> str:
    compile_all = [ms for r in rows for ms in r["compile_ms"]]
    legacy_all = [ms for r in rows for ms in r["legacy_ms"]]
    lines = [
        "# Context Engine baseline — legacy blocks vs compiled packet",
        "",
        f"Data set: **{label}** · queries: {len(rows)} · runs per query: {max(1, args.runs)} · "
        f"model budget: `{args.model}`, window {args.context_length}, "
        f"output reserve {args.max_output_tokens}"
        + (f", packet budget {args.packet_budget}" if args.packet_budget else "") + ".",
        "",
        "Generated by `scripts/bench_context_engine.py` (offline, no model). Legacy = "
        "learned-memory block + saved-memory preface as the agent prompt injects them "
        "with the engine off; packet = a shadow compile rendered as the live path "
        "renders it. Tokens use one estimator for both sides. Personal-document RAG "
        "is excluded on both sides (it needs an embedding model).",
        "",
        "| # | query | legacy tok | packet tok | packet sections | packet sources | "
        "legacy refs | compile p50 ms | recall legacy | recall packet (+recoverable) |",
        "|---|---|---:|---:|---|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        sources = ", ".join(f"{k}:{v}" for k, v in sorted(r["packet_sources"].items())) or "—"
        recall_packet = _pct(r["recall_packet"])
        if r["recall_packet_recoverable"] is not None and r["recall_packet_recoverable"] != r["recall_packet"]:
            recall_packet += f" ({_pct(r['recall_packet_recoverable'])})"
        lines.append(
            f"| {r['n']} | {_cell(r['query'])} | {r['legacy_tokens']} | {r['packet_tokens']} | "
            f"{_cell(', '.join(r['sections']) or '—', 60)} | {_cell(sources, 60)} | "
            f"{len(r['legacy_refs'])} | {percentile(r['compile_ms'], 50):.1f} | "
            f"{_pct(r['recall_legacy'])} | {recall_packet} |"
            + (f" ⚠ {r['error']}" if r.get("error") else ""))
    total_legacy = sum(r["legacy_tokens"] for r in rows)
    total_packet = sum(r["packet_tokens"] for r in rows)
    lines += [
        "",
        "## Summary",
        "",
        f"- Tokens: legacy {total_legacy} · packet {total_packet} "
        f"(mean per query {total_legacy / max(1, len(rows)):.0f} vs "
        f"{total_packet / max(1, len(rows)):.0f}).",
        f"- Compile latency: p50 {percentile(compile_all, 50):.1f} ms · "
        f"p95 {percentile(compile_all, 95):.1f} ms "
        f"(legacy block build p50 {percentile(legacy_all, 50):.1f} ms · "
        f"p95 {percentile(legacy_all, 95):.1f} ms).",
        f"- Recall of expected refs: legacy {_pct(_mean([r['recall_legacy'] for r in rows]))} · "
        f"packet {_pct(_mean([r['recall_packet'] for r in rows]))} · "
        f"packet incl. recoverable omissions "
        f"{_pct(_mean([r['recall_packet_recoverable'] for r in rows]))}.",
        f"- Degraded packets: {sum(1 for r in rows if r.get('degraded'))} of {len(rows)}; "
        f"budget omissions recoverable with `context_recall`: "
        f"{sum(len(r['recoverable']) for r in rows)}.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    temp_dir = ""
    if args.demo:
        temp_dir = tempfile.mkdtemp(prefix="ctx-bench-demo-")
        data_dir = temp_dir
        args.owner = args.owner or DEMO_OWNER
        label = args.label or "demo (invented content)"
    else:
        data_dir = args.data_dir or os.environ.get("ODYSSEUS_DATA_DIR", "")
        label = args.label or "custom data dir"
        if args.snapshot and data_dir:
            temp_dir = tempfile.mkdtemp(prefix="ctx-bench-snap-")
            shutil.copytree(data_dir, os.path.join(temp_dir, "data"))
            data_dir = os.path.join(temp_dir, "data")
    if data_dir:
        # Before any `src` import: DATA_DIR is read at import time.
        os.environ["ODYSSEUS_DATA_DIR"] = os.path.abspath(data_dir)
    sys.path.insert(0, ROOT)
    import logging

    logging.basicConfig(level=logging.ERROR)
    try:
        if args.demo:
            seed_demo(data_dir)
        with open(args.queries, encoding="utf-8") as fh:
            queries = json.load(fh)
        if not isinstance(queries, list):
            raise SystemExit("the query file must hold a JSON list")
        rows = asyncio.run(measure(queries, args))
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            report = render_markdown(rows, args, label)
            print(report)
            if args.write:
                os.makedirs(os.path.dirname(os.path.abspath(args.write)), exist_ok=True)
                with open(args.write, "w", encoding="utf-8") as fh:
                    fh.write(report)
                print(f"\nwritten: {os.path.relpath(os.path.abspath(args.write), ROOT)}",
                      file=sys.stderr)
        return 0
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
