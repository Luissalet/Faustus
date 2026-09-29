"""Offline OpenViking/Hindsight pilot; real compiler and SQLite/hash recall.

Run from the repository root with `venv/Scripts/python.exe -m
scripts.eval_retrieval_pilot --output <json>`. No generation provider is used.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

from src.bench import memory_recall as bench
from src.context_engine import store
from src.context_engine.adapters.memory import MemoryEngineSource
from src.context_engine.adapters.sessions import SessionSource, history_scope
from src.context_engine.cache import WorkingSet
from src.context_engine.compiler import ContextCompiler
from src.context_engine.contracts import (
    MANDATORY_SECTIONS, ContextActor, ContextCandidate, ContextExecution, ContextRequest, ContextTask,
)


class CountedMemory(MemoryEngineSource):
    def __init__(self):
        self.calls = 0

    async def search(self, request):
        self.calls += 1
        return await super().search(request)


async def context_pilot(rounds=12):
    """Cold working sets, warmed process; no artificial delay or source stubs."""
    history = [{"role": "user", "content": "We discussed the Atlas release yesterday."},
               {"role": "assistant", "content": "The pending release needs a review."}]
    mandatory_bodies = (
        "Never disclose credentials.", "Only read the authorized owner data.",
        "Help Alice with Atlas.", "The release is awaiting approval.",
        "Keep the agreed review requirement.")
    mandatory = [ContextCandidate(
        candidate_id=f"pilot-{section}", source_type="instruction",
        source_ref=f"instruction:{section}", title=section,
        body=body, section=section, lanes=("mandatory",),
        authority="system_policy", trust_class="human_explicit")
        for section, body in zip(MANDATORY_SECTIONS, mandatory_bodies)]
    results = {}
    with tempfile.TemporaryDirectory(prefix="faustus_context_pilot_") as directory:
        store.use_path(str(Path(directory) / "context.db"))
        try:
            with bench._isolated_engine():
                keys = bench._insert_corpus()
                # Freeze the adapter's clock too; fixtures are anchored in January.
                real_search = bench.memory_engine.search
                def anchored_search(*args, **kwargs):
                    kwargs["now"] = bench.ANCHOR
                    return real_search(*args, **kwargs)
                with patch.object(bench.memory_engine, "search", anchored_search):
                    for label, query in (("greeting", "¡Hola!"),
                                         ("memory_question", "Hello, what editor does Alice prefer?")):
                        timings, calls, packets = [], [], []
                        for index in range(rounds + 1):
                            memory = CountedMemory()
                            compiler = ContextCompiler(
                                sources=[memory, SessionSource()], cache=WorkingSet(),
                                clock=lambda: bench.ANCHOR)
                            request = ContextRequest(
                                request_id=f"pilot-{label}-{index}", consumer="chat",
                                actor=ContextActor(agent_id="pilot", role="chat", model="offline"),
                                execution=ContextExecution(owner=bench.OWNER, session_id="pilot",
                                                           project_id=bench.PROJECT),
                                task=ContextTask(query=query))
                            with history_scope("pilot", bench.OWNER, history):
                                start = time.perf_counter()
                                packet = await compiler.compile(
                                    request, mandatory=mandatory, context_length=8192,
                                    window_known=True, max_output_tokens=512)
                                elapsed = (time.perf_counter() - start) * 1000
                            bodies = [item.body for item in packet.items()]
                            refs = [item.source_ref for item in packet.items()]
                            assert all(any(item.body in body for body in bodies) for item in mandatory), packet.warnings
                            assert all(any(row["content"] in body for body in bodies) for row in history)
                            assert memory.calls == (label == "memory_question")
                            if label == "memory_question":
                                assert f"mem:{keys['pref_editor']}" in refs, refs
                            assert not packet.degraded, packet.warnings
                            if index:  # first compile is a warm-up, not a timing sample
                                timings.append(elapsed)
                                calls.append(memory.calls)
                                packets.append(packet.tokens())
                        results[label] = dict(
                            query=query, samples=rounds, memory_search_calls=calls,
                            latency_p50_ms=round(statistics.median(timings), 3),
                            latency_min_ms=round(min(timings), 3),
                            latency_max_ms=round(max(timings), 3),
                            packet_tokens=packets, mandatory_and_history_preserved=True)
        finally:
            store.use_path(None)
    return results


def group_recall(report):
    """Group existing query evidence without counting negative probes as misses."""
    corpus = {row["key"]: row for row in bench.CORPUS}
    groups = defaultdict(list)
    for row in report["per_query"]:
        expected = row["expected"]
        if not expected:
            category = "isolation_negative" if "cross_tenant" in row["key"] else "expiry_negative"
        elif any(key.startswith("contra_") for key in expected):
            category = "corrected_facts"
        else:
            types = {corpus[key].get("type", "fact") for key in expected}
            category = next(iter(types)) if len(types) == 1 else "mixed_equivalents"
        groups[category].append(row)
    output = {}
    for category, rows in sorted(groups.items()):
        positive = [row for row in rows if row["expected"]]
        output[category] = dict(
            queries=len(rows), positive_queries=len(positive),
            mrr=round(statistics.mean(row["reciprocal_rank"] for row in positive), 6) if positive else None,
            hit_at_5=round(statistics.mean(row["per_k"]["5"]["hit"] for row in positive), 6) if positive else None,
            recall_at_5=round(statistics.mean(row["per_k"]["5"]["recall"] for row in positive), 6) if positive else None,
            forbidden_leaks=sum(len(row["leaked_keys"]) for row in rows),
            missed_queries=[row["key"] for row in positive if not row["per_k"]["5"]["hit"]])
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    recall = bench.run()
    report = dict(
        methodology="Offline compiler + actual memory/session adapters; isolated SQLite; hash embeddings; no LLM latency.",
        context=asyncio.run(context_pilot()), recall_by_question_type=group_recall(recall),
        recall=recall, lifecycle=bench.run_lifecycle())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "recall"}, indent=2))


if __name__ == "__main__":
    main()
