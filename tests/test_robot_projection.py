"""Robot-mode projections — the lean view, and the savings it really buys.

`src/robot_projection.py` exists because robot mode did not deliver: measured
against a running instance, `?format=toon` came back BIGGER than the plain JSON
body on three of four endpoints (memory items 1.15x, objectives 1.24x, usage
1.23x) and saved 7 % on the fourth. Re-encoding the browser's payload cannot
win — every row in it carries a list or an object, so TOON's tabular form never
fires and its indentation costs more than JSON's braces.

So the fixtures here are REALISTIC — the shapes the endpoints really build,
with the nested fields populated: memory items carrying three helpful and two
harmful events apiece, objectives with deps and hints and a per-id scores
object, a guard log whose receipts carry 192 characters of chain hash each, a
two-GPU box with three models loaded, a job whose workers list files and static
checks. Against those, every projected payload must come back under 0.75 of the
plain JSON body, and the tables must actually be tables.

The other half of the file is the defensive contract: a projection meeting a
payload it did not expect answers with that payload, never an exception.
"""
from __future__ import annotations

import json

import pytest

from src import robot_envelope, robot_projection, toon


# ── the fixtures: what the endpoints really build ───────────────────────────

def memory_payload(n: int = 5) -> dict:
    """`GET /api/memory-engine/items?limit=5` — public_item() rows, each with
    its evidence span and its 3 helpful / 2 harmful feedback events."""
    items = []
    for i in range(n):
        ident = f"{i:02d}" * 16
        items.append({
            "id": ident,
            "owner": "luis",
            "project": "/srv/covernet",
            "level": ["procedural", "semantic", "episodic"][i % 3],
            "text": "Always run the project test suite before reporting a task done, "
                    f"and never edit the generated client in vendor/{i}",
            "category": "testing",
            "trust_class": ["human_explicit", "agent_assertion", "observed"][i % 3],
            "trust": 0.85,
            "confidence": 0.85,
            "status": "active",
            "maturity": "established",
            "evidence": [
                {"kind": "chat", "session_id": f"sess-{i:04d}",
                 "excerpt": "the user asked for the tests to be run first, twice in a row"},
                {"kind": "tool", "ref": f"pytest -q tests/test_cart.py::case_{i}",
                 "excerpt": "exit 1 — 2 failed, 118 passed"},
            ],
            "helpful": [
                {"ts": f"2026-08-2{k}T09:14:0{i}+00:00", "weight": 1.0,
                 "reason": "the worker followed it and the suite stayed green",
                 "ref": f"dispatch:{i:02d}{k}bc4de5f6"} for k in range(1, 4)
            ],
            "harmful": [
                {"ts": f"2026-08-2{k}T11:02:0{i}+00:00", "weight": 1.0,
                 "reason": "held for a repo with no test runner at all",
                 "ref": f"dispatch:{i:02d}{k}ff1122aa"} for k in range(1, 3)
            ],
            "inverted_from": "",
            "created_at": "2026-07-01T09:00:00+00:00",
            "updated_at": "2026-08-30T12:34:56+00:00",
            "last_accessed": "2026-09-01T08:00:00+00:00",
            "access_count": 12 + i,
            "id8": ident[:8],
            "effective_score": 0.7312,
            "harmful_ratio": 0.4,
            "helpful_count": 3,
            "harmful_count": 2,
            "distinct_helpful_refs": 3,
        })
    return {
        "status": "success",
        "items": items,
        "stats": {"total": n, "active": n, "anti_pattern": 0, "deprecated": 0,
                  "semantic_lane": False},
        "levels": ["procedural", "semantic", "episodic"],
        "trust_classes": {"human_explicit": 0.85, "agent_assertion": 0.5,
                          "observed": 0.65, "inferred": 0.35},
    }


def objectives_payload(n: int = 6, log_rows: int = 12) -> dict:
    """`GET /api/projects/{id}/objectives` — dashboard_payload(): objectives
    with their deps inlined, the edge list, the per-id impact scores with
    their five components, and the audit tail."""
    objectives = []
    for i in range(1, n + 1):
        deps = [f"OBJ-{i - 1}"] if i > 1 else []
        if i == n:
            deps = [f"OBJ-{i - 1}", f"OBJ-{i - 2}"]
        objectives.append({
            "t": "obj", "id": f"OBJ-{i}",
            "title": f"Ship the {i}th slice of the billing API",
            "status": ["open", "in_progress", "done", "blocked"][i % 4],
            "priority": (i % 4) + 1, "owner": "user",
            "notes": "the cart totals must keep matching the invoice totals",
            "created_at": "2026-08-01T10:00:00+00:00",
            "updated_at": "2026-08-30T12:34:56+00:00",
            "last_actor": "agent", "deps": deps,
        })
    return {
        "objectives": objectives,
        "edges": [{"from": o["id"], "to": d} for o in objectives for d in o["deps"]],
        "scores": {o["id"]: {
            "score": round(0.4231 + i / 100.0, 4),
            "hint": "blocks two others and has not moved in 9 days" if i % 2 else None,
            "components": {"pagerank": 0.1234, "betweenness": 0.0812,
                           "blocker_ratio": 0.25, "staleness": 0.9,
                           "priority_boost": 0.5},
        } for i, o in enumerate(objectives)},
        "log": [{"ts": f"2026-08-30T12:{i:02d}:56+00:00", "kind": "delta",
                 "actor": "agent", "op": "EDIT", "id": f"OBJ-{(i % n) + 1}",
                 "fields": {"status": "done", "priority": 2},
                 "rationale": "the worker finished the task and the suite passed",
                 "session": "sess-0001"} for i in range(log_rows)],
    }


def guard_log_payload(n: int = 25) -> dict:
    """`GET /api/command-guard/log?limit=25` — append_receipt() records: the
    hash chain is three 64-character digests per receipt, and only some of
    them carry a `note`."""
    receipts = []
    for i in range(n):
        record = {
            "ts": f"2026-08-30T12:{i:02d}:56.123456+00:00",
            "session": f"sess-{i:04d}",
            "tool": ["bash", "python", "bash"][i % 3],
            "command_sha256": f"{i:02x}" * 32,
            "command_head": f"rm -rf build/stage-{i} && pytest -q tests/test_cart.py",
            "tier": ["DANGEROUS", "SAFE", "CAUTION"][i % 3],
            "rule": ["fs.rm_rf", "", "fs.write"][i % 3],
            "action": ["blocked", "allowed", "allowlisted"][i % 3],
            "prev_hash": f"{(i + 1) % 256:02x}" * 32,
            "hash": f"{(i + 7) % 256:02x}" * 32,
        }
        if i % 4 == 0:
            record["note"] = "matched the fs pack; the allowlist entry had expired"
        receipts.append(record)
    return {"status": "success", "receipts": receipts,
            "chain": {"ok": True, "length": n, "broken_at": None}}


def usage_payload() -> dict:
    """`GET /api/system/usage` — two cards, three models loaded (one split
    across both), one orphaned runner."""
    models = [
        {"name": "qwen3.5:9b", "size": 9_123_456_789, "size_vram": 9_123_456_789,
         "gpu_pct": 100, "cpu_pct": 0, "context_length": 32768,
         "expires_at": "2026-09-03T10:05:00.123456Z", "parameter_size": "9.2B",
         "quantization": "Q4_K_M", "family": "qwen3", "gpus": [0],
         "placement": "single", "per_gpu": [{"index": 0, "bytes": 9_123_456_789}]},
        {"name": "qwen3.8:27b-q8_0", "size": 29_123_456_789, "size_vram": 24_000_000_000,
         "gpu_pct": 82, "cpu_pct": 18, "context_length": 16384,
         "expires_at": "2026-09-03T10:11:00.123456Z", "parameter_size": "27B",
         "quantization": "Q8_0", "family": "qwen3", "gpus": [0, 1],
         "placement": "split", "per_gpu": [{"index": 0, "bytes": 12_000_000_000},
                                           {"index": 1, "bytes": 12_000_000_000}]},
        {"name": "nomic-embed-text:latest", "size": 274_000_000, "size_vram": 274_000_000,
         "gpu_pct": 100, "cpu_pct": 0, "context_length": 8192,
         "expires_at": "2026-09-03T10:02:00.123456Z", "parameter_size": "137M",
         "quantization": "F16", "family": "nomic-bert", "gpus": [1],
         "placement": "single", "per_gpu": [{"index": 1, "bytes": 274_000_000}]},
    ]
    gpus = [
        {"index": 0, "name": "NVIDIA GeForce RTX 4090", "util": 87, "mem_used": 21234,
         "mem_total": 24564, "mem_free": 3330, "temp": 61, "power": 320.5,
         "power_limit": 450.0, "uuid": "GPU-1b4c2f90-1111-2222-3333-444455556666",
         "bus_id": "00000000:01:00.0",
         "models": [{"name": "qwen3.5:9b", "bytes": 9_123_456_789},
                    {"name": "qwen3.8:27b-q8_0", "bytes": 12_000_000_000}],
         "runner_pids": [15948, 15949]},
        {"index": 1, "name": "NVIDIA GeForce RTX 3090", "util": 12, "mem_used": 12310,
         "mem_total": 24576, "mem_free": 12266, "temp": 44, "power": 102.0,
         "power_limit": 350.0, "uuid": "GPU-9f8e7d60-aaaa-bbbb-cccc-ddddeeeeffff",
         "bus_id": "00000000:02:00.0",
         "models": [{"name": "qwen3.8:27b-q8_0", "bytes": 12_000_000_000},
                    {"name": "nomic-embed-text:latest", "bytes": 274_000_000}],
         "runner_pids": [15949, 16022]},
    ]
    return {
        "ts": 1767268496.1234567,
        "ollama": {"reachable": True, "base": "http://127.0.0.1:11434", "models": models},
        "gpu": gpus,
        "gpu_pool": {"count": 2, "mem_used": 33544, "mem_total": 49140, "mem_free": 15596,
                     "util": 87, "util_avg": 49.5, "power": 422.5, "power_limit": 800.0,
                     "temp": 61, "names": ["NVIDIA GeForce RTX 4090",
                                           "NVIDIA GeForce RTX 3090"]},
        "orphans": [{"pid": 14002, "name": "llama-server", "started": "2026-09-03T08:41:12",
                     "gpus": [1], "bytes": 3_400_000_000,
                     "blob": "sha256-9f1c8e0a5b6d7e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f"}],
        "gpu_mem": {"supported": True,
                    "ollama": {"shared": 0, "dedicated": 7_600_000_000, "spilling": False}},
        "sysmem_fallback": {"exposed": False, "manual_only": True, "steps": [
            "open the NVIDIA Control Panel",
            "Manage 3D settings → CUDA — Sysmem Fallback Policy",
            "set it to 'Prefer No Sysmem Fallback' for ollama.exe",
        ]},
        "cpu": {"percent": 12.5, "count": 32},
        "ram": {"used": 40_100_000_000, "total": 137_000_000_000, "percent": 29.3},
        "errors": [],
    }


def dispatch_payload() -> dict:
    """`GET /api/dispatch/{id}` — dispatch.compact() for a finished job with
    three workers, evidence, a failed verification and a fix round."""
    workers = []
    for i in range(1, 4):
        workers.append({
            "name": f"worker-{i}", "role": "worker", "status": ["done", "done", "error"][i - 1],
            "stop_reason": "end_turn", "error": None if i < 3 else "the model stopped mid-edit",
            "rounds": 3 + i, "tool_calls": 9 + i, "failed_calls": i - 1,
            "files_changed": [f"src/cart_{i}.py", f"tests/test_cart_{i}.py"],
            "input_tokens": 41_000 + i * 900, "output_tokens": 2_300 + i * 40,
            "duration_s": 91.4 + i, "model": "qwen3.5:9b",
            "summary": "Added apply_tax() to the cart module and a regression test "
                       "covering the zero-rate case; the suite passes locally.",
            "session_id": f"sess-{i:04d}", "outcome": ["success", "success", "failed"][i - 1],
            "static_checks": {"checked": 12, "failed": [
                {"path": f"src/cart_{i}.py", "error": "E501 line too long (103 > 99)"}]},
            "supervisor": ["round 3: the worker re-read the file it had just written"],
        })
    return {
        "id": "9f2c1b7ad4e0", "owner": "luis", "title": "Workers · add apply_tax",
        "status": "partial", "error": "",
        "verdict": "2/3 workers done (1 error) · 6 files changed on disk · verification failed",
        "workspace": "/srv/covernet", "model": "qwen3.5:9b",
        "session_id": "sess-0001", "chat_url": "/#sess-0001",
        "created": 1767268400.1, "started": 1767268401.4, "finished": 1767268700.9,
        "duration_s": 299.5,
        "tasks": [{"name": f"worker-{i}",
                   "instruction": "Add apply_tax(subtotal, rate) to the cart module, "
                                  "with a regression test for the zero-rate case and "
                                  "the rounding rule the invoice code already uses.",
                   "files": [f"src/cart_{i}.py"], "model": None} for i in range(1, 4)],
        "parallel": True, "reviewer": False, "max_rounds": 20, "timeout_s": 900,
        "verify": "auto", "verify_scope": "related", "fix_rounds": 1,
        "result": {
            "workers": workers,
            "files_changed": ["src/cart_1.py", "src/cart_2.py", "src/cart_3.py",
                              "tests/test_cart_1.py", "tests/test_cart_2.py",
                              "tests/test_cart_3.py"],
            "claimed_only": ["docs/cart.md"],
            "totals": {"tool_calls": 33, "failed_calls": 3, "rounds": 15,
                       "input_tokens": 128_400, "output_tokens": 7_140, "errors": 1},
            "changes": {"source": "checkpoint", "count": 6,
                        "added": ["tests/test_cart_1.py", "tests/test_cart_2.py",
                                  "tests/test_cart_3.py"],
                        "modified": ["src/cart_1.py", "src/cart_2.py", "src/cart_3.py"],
                        "deleted": [], "truncated": False,
                        "git": {"repo": True, "dirty_count": 6,
                                "shortstat": "6 files changed, 214 insertions(+), 8 deletions(-)"}},
            "verification": {
                "mode": "auto", "ran": True, "ok": False, "inconclusive": False,
                "kind": "pytest", "command": "python -m pytest -q tests/test_cart_3.py",
                "scope": "related", "exit_code": 1, "timed_out": False, "duration_s": 12.4,
                "summary": "1 failed, 118 passed in 12.40s",
                "failures": ["tests/test_cart_3.py::test_zero_rate — AssertionError: 0.0 != 0"],
                "output_tail": ("=" * 60 + "\nFAILED tests/test_cart_3.py::test_zero_rate\n"
                                + "self = <TestCart object>\n" * 20),
                "related_files": ["tests/test_cart_3.py"],
                "new_failures": ["tests/test_cart_3.py::test_zero_rate"],
            },
            "convergence": {"score": 0.81, "confidence": "moderate", "converged": False,
                            "rounds": 2, "reason": "rounds are still changing things",
                            "components": {"size": 0.7, "velocity": 0.9, "similarity": 0.8}},
            "exit_code": 1,
        },
    }


def events_payload(n: int = 30) -> dict:
    """`GET /api/dispatch/{id}/events` — the board's tail: job lines and the
    harness's per-worker ticks, which carry different keys."""
    events = [{"event": "job", "name": "job", "ts": 1767268401.4,
               "message": "checkpointing the workspace"}]
    for i in range(n):
        name = f"worker-{(i % 3) + 1}"
        if i % 3 == 0:
            events.append({"event": "tool", "name": name, "id": f"sub-{i}",
                           "ts": 1767268410.0 + i, "round": (i % 5) + 1,
                           "tool": "write_file", "elapsed_s": 4.2 + i})
        elif i % 3 == 1:
            events.append({"event": "tick", "name": name, "ts": 1767268410.0 + i,
                           "round": (i % 5) + 1, "elapsed_s": 4.2 + i, "idle_s": 0.4,
                           "last_tool": "run_command", "stalled": False})
        else:
            events.append({"event": "round", "name": name, "ts": 1767268410.0 + i,
                           "round": (i % 5) + 1, "status": "running",
                           "message": f"round {(i % 5) + 1} of 20"})
    return {"id": "9f2c1b7ad4e0", "status": "partial", "events": events}


# ── the measurement ─────────────────────────────────────────────────────────

def _plain(payload) -> str:
    """The bytes the endpoint answers a browser with (Starlette's JSONResponse
    writes compact separators)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _toon(data) -> str:
    """The bytes `?format=toon` answers with: the envelope, in TOON."""
    return toon.encode(robot_envelope.envelope(data))


def ratio(payload, projected) -> float:
    """What a caller actually measures: `?format=toon` over the plain body."""
    return len(_toon(projected)) / len(_plain(payload))


CASES = [
    ("/api/memory-engine/items?limit=5", memory_payload(), robot_projection.memory_items),
    ("/api/projects/{id}/objectives", objectives_payload(), robot_projection.objectives),
    ("/api/command-guard/log?limit=25", guard_log_payload(), robot_projection.guard_log),
    ("/api/system/usage", usage_payload(), robot_projection.system_usage),
    ("/api/dispatch/{id}", dispatch_payload(), robot_projection.dispatch_status),
    ("/api/dispatch/{id}/events", events_payload(), robot_projection.dispatch_events),
]


@pytest.mark.parametrize("endpoint,payload,project", CASES,
                         ids=[c[0] for c in CASES])
def test_the_lean_projection_really_is_under_three_quarters(endpoint, payload, project):
    """The claim robot mode failed to keep, on the payloads the endpoints
    really build: `?format=toon` must cost under 0.75 of the plain JSON body.
    Re-encoding the full payload — what shipped — is measured beside it, and
    it is the thing that was over 1.0."""
    lean = ratio(payload, project(payload))
    shipped = ratio(payload, payload)
    assert lean < 0.75, (endpoint, lean)
    assert lean < shipped, (endpoint, lean, shipped)


@pytest.mark.parametrize("endpoint,payload,project", CASES,
                         ids=[c[0] for c in CASES])
def test_every_projected_array_is_a_real_toon_table(endpoint, payload, project):
    """The savings come from ONE place: an array of rows sharing a key set,
    all of whose values are scalars, written as a header plus a line per row.
    So every cell a projection emits must be a scalar, and every array of two
    or more rows must carry its `[N]{cols}:` header — a fall back to `- `
    items means the row shape has regressed. (TOON needs two rows to justify
    a header, so a one-row array staying items is the format's own rule, not
    a projection that slipped.)"""
    projected = project(payload)
    text = toon.encode(projected)
    for key, value in projected.items():
        if not (isinstance(value, list) and value and isinstance(value[0], dict)):
            continue
        for row in value:
            assert set(row) == set(value[0]), (endpoint, key, row)
            for column, cell in row.items():
                assert cell is None or isinstance(cell, (str, int, float, bool)), \
                    (endpoint, key, column, cell)
        if len(value) > 1:
            assert f"{key}[{len(value)}]{{" in text, (endpoint, key, text[:400])


@pytest.mark.parametrize("endpoint,payload,project", CASES,
                         ids=[c[0] for c in CASES])
def test_a_projection_is_deterministic_and_round_trips(endpoint, payload, project):
    """Same payload, same bytes — and the lean form is still TOON that decodes
    back to exactly the object Faustus meant to send."""
    once, twice = project(payload), project(payload)
    assert once == twice
    assert toon.encode(once) == toon.encode(twice)
    assert toon.decode(_toon(once)) == robot_envelope.envelope(once)


# ── what each projection must keep ──────────────────────────────────────────

def test_memory_items_keep_the_decision_fields_and_drop_the_event_arrays():
    lean = robot_projection.memory_items(memory_payload())
    row = lean["items"][0]
    assert list(row) == list(robot_projection._ITEM_COLUMNS)
    assert row["id8"] == "00000000" and len(row["id8"]) == 8
    assert row["helpful_count"] == 3 and row["harmful_count"] == 2
    assert row["effective_score"] == 0.7312 and row["harmful_ratio"] == 0.4
    assert row["updated_at"] == "2026-08-30T12:34:56+00:00"
    assert "evidence" not in row and "helpful" not in row and "created_at" not in row
    # the counts the header needs stay; the enum tables the page paints do not
    assert lean["stats"]["total"] == 5
    assert "levels" not in lean and "trust_classes" not in lean and "status" not in lean


def test_objectives_fold_the_score_in_and_join_the_deps():
    payload = objectives_payload()
    lean = robot_projection.objectives(payload)
    assert list(lean["objectives"][0]) == list(robot_projection._OBJECTIVE_COLUMNS)
    last = lean["objectives"][-1]
    assert last["id"] == "OBJ-6" and last["blocked_by"] == "OBJ-5,OBJ-4"
    assert lean["objectives"][0]["blocked_by"] == ""
    # the per-id scores object is gone: each score sits in its own row
    assert "scores" not in lean
    assert lean["objectives"][1]["score"] == payload["scores"]["OBJ-2"]["score"]
    assert lean["objectives"][1]["hint"] == payload["scores"]["OBJ-2"]["hint"]
    assert lean["objectives"][0]["hint"] == ""      # a None hint is an empty cell
    # edges stay their own table, and the audit tail keeps one row per record
    assert lean["edges"][0] == {"from": "OBJ-2", "to": "OBJ-1"}
    assert lean["log"][0]["note"] == "the worker finished the task and the suite passed"
    assert len(lean["log"]) == len(payload["log"])


def test_a_log_record_without_a_rationale_shows_its_fields_as_pairs():
    payload = objectives_payload(log_rows=1)
    payload["log"][0].pop("rationale")
    lean = robot_projection.objectives(payload)
    assert lean["log"][0]["note"] == "status=done;priority=2"


def test_guard_receipts_drop_the_chain_hashes_but_keep_the_verdict():
    payload = guard_log_payload()
    lean = robot_projection.guard_log(payload)
    row = lean["receipts"][0]
    assert list(row) == list(robot_projection._RECEIPT_COLUMNS)
    assert row["hash8"] == payload["receipts"][0]["hash"][:8]
    assert row["command_head"].startswith("rm -rf build/stage-0")
    assert row["note"].startswith("matched the fs pack")
    # every row has the note column, including the three in four that had none
    assert lean["receipts"][1]["note"] == ""
    assert all("prev_hash" not in r and "command_sha256" not in r
               for r in lean["receipts"])
    assert lean["chain"] == {"ok": True, "length": 25, "broken_at": None}


def test_system_usage_is_three_tables_and_the_pool_figures():
    lean = robot_projection.system_usage(usage_payload())
    assert list(lean["gpus"][0]) == list(robot_projection._GPU_COLUMNS)
    assert list(lean["models"][0]) == list(robot_projection._MODEL_COLUMNS)
    assert list(lean["orphans"][0]) == list(robot_projection._ORPHAN_COLUMNS)
    assert lean["models"][1]["gpus"] == "0,1" and lean["models"][1]["placement"] == "split"
    assert lean["pool"]["mem_free"] == 15596 and "names" not in lean["pool"]
    assert lean["ollama"] == {"reachable": True, "loaded": 3}
    assert lean["gpu_mem"]["spilling"] is False
    assert lean["cpu"] == {"percent": 12.5, "count": 32}
    # the per-card model list and the driver-panel prose are gone
    assert "models" not in lean["gpus"][0] and "uuid" not in lean["gpus"][0]
    assert lean["sysmem_fallback_exposed"] is False


def test_dispatch_status_flattens_the_workers_and_keeps_the_verdict():
    lean = robot_projection.dispatch_status(dispatch_payload())
    assert list(lean["workers"][0]) == list(robot_projection._WORKER_COLUMNS)
    assert lean["workers"][0]["files"] == 2 and lean["workers"][0]["checks_failed"] == 1
    assert lean["workers"][2]["error"] == "the model stopped mid-edit"
    assert lean["verdict"].startswith("2/3 workers done")
    # the result block is unwrapped, and the verdict scalars survive it
    assert "result" not in lean and lean["exit_code"] == 1
    assert lean["verification"]["ok"] is False
    assert lean["verification"]["command"].endswith("tests/test_cart_3.py")
    assert lean["verification"]["failures"][0].startswith("tests/test_cart_3.py::test_zero_rate")
    assert "output_tail" not in lean["verification"]
    assert lean["convergence"]["converged"] is False
    assert "components" not in lean["convergence"]
    assert lean["changes"] == {"source": "checkpoint", "count": 6, "truncated": False}
    assert len(lean["files_changed"]) == 6 and lean["claimed_only"] == ["docs/cart.md"]
    # what the coordinator itself sent does not come back
    assert "tasks" not in lean and "max_rounds" not in lean and "session_id" not in lean


def test_a_running_job_keeps_its_progress_as_a_table():
    payload = dispatch_payload()
    payload["status"] = "running"
    payload["progress"] = {"worker-1": {"last_event": "tool", "round": 3, "elapsed_s": 41.2,
                                        "last_tool": "write_file", "stalled": False},
                           "worker-2": {"last_event": "queued"}}
    payload["wait_again"] = True
    payload["ceiling_s"] = 1900
    payload["phase"] = "running the verification"
    lean = robot_projection.dispatch_status(payload)
    assert [r["name"] for r in lean["progress"]] == ["worker-1", "worker-2"]
    assert list(lean["progress"][0]) == list(robot_projection._PROGRESS_COLUMNS)
    assert lean["progress"][1]["round"] is None and lean["progress"][1]["stalled"] is False
    assert lean["wait_again"] is True and lean["ceiling_s"] == 1900
    assert lean["phase"] == "running the verification"
    assert f"progress[2]{{{','.join(robot_projection._PROGRESS_COLUMNS)}}}:" \
        in toon.encode(lean)


def test_events_become_one_table_though_the_emitters_disagree_on_keys():
    payload = events_payload()
    lean = robot_projection.dispatch_events(payload)
    assert len(lean["events"]) == len(payload["events"])
    assert list(lean["events"][0]) == list(robot_projection._EVENT_COLUMNS)
    assert lean["events"][0]["message"] == "checkpointing the workspace"
    assert lean["events"][2]["tool"] == "run_command"   # a tick's `last_tool`
    assert lean["id"] == "9f2c1b7ad4e0" and lean["status"] == "partial"


# ── the defensive contract ──────────────────────────────────────────────────

class Nasty:
    """An object whose every dunder explodes — the thing a cell must survive."""

    def __str__(self):
        raise ValueError("boom")

    def __eq__(self, other):
        raise ValueError("boom")

    def __hash__(self):
        return 1


JUNK = [
    {}, {"items": None}, {"items": "not a list"}, {"items": [None, 3, "x"]},
    {"objectives": [{"id": None, "deps": {"a": 1}}], "scores": []},
    {"receipts": [{"corrupt_line": "}{ broken json"}], "chain": None},
    {"gpu": [[]], "ollama": 7, "gpu_pool": "none"},
    {"result": "not a dict", "progress": [1, 2]},
    {"events": [{"ts": object()}]},
    {"items": [{"text": Nasty(), "id": Nasty()}],
     "objectives": [{"id": Nasty(), "deps": [Nasty()]}],
     "receipts": [{"ts": Nasty()}], "gpu": [{"index": Nasty()}],
     "result": {"workers": [{"name": Nasty()}]}, "events": [{"ts": Nasty()}]},
]


@pytest.mark.parametrize("project", [
    robot_projection.memory_items, robot_projection.objectives,
    robot_projection.guard_log, robot_projection.system_usage,
    robot_projection.dispatch_status, robot_projection.dispatch_events,
])
@pytest.mark.parametrize("payload", JUNK, ids=range(len(JUNK)))
def test_no_projection_can_raise_into_a_working_read(project, payload):
    """Robot mode may lose the compaction on a payload it did not expect; it
    may never turn a 200 into a 500. Whatever comes back must also encode."""
    out = project(payload)
    assert isinstance(out, (dict, list, str, int, float, type(None)))
    assert isinstance(toon.encode(out), str)


@pytest.mark.parametrize("project", [
    robot_projection.memory_items, robot_projection.objectives,
    robot_projection.guard_log, robot_projection.system_usage,
    robot_projection.dispatch_status, robot_projection.dispatch_events,
])
def test_a_payload_that_is_not_an_object_comes_back_untouched(project):
    for value in ([1, 2, 3], "text", None, 7):
        assert project(value) == value


def test_a_projection_that_explodes_answers_with_the_payload(monkeypatch):
    """The guarantee spelled out: if the row builder itself raises, the read
    still answers — with the full payload, not an error."""
    def boom(item):
        raise RuntimeError("the row builder is broken")

    monkeypatch.setattr(robot_projection, "_memory_row", boom)
    payload = memory_payload(2)
    assert robot_projection.memory_items(payload) == payload


def test_a_text_cell_is_squashed_to_one_line():
    """A table row IS a line: a cell holding a newline would be quoted and
    escaped, costing more than it saves."""
    payload = memory_payload(2)
    payload["items"][0]["text"] = "first line\n\tsecond   line   \n"
    lean = robot_projection.memory_items(payload)
    assert lean["items"][0]["text"] == "first line second line"


def test_the_measured_ratios_are_recorded_for_the_docs():
    """The numbers website/fable-workers.md quotes, computed from the same
    fixtures, so the table cannot drift away from the code."""
    measured = {endpoint: round(ratio(payload, project(payload)), 2)
                for endpoint, payload, project in CASES}
    assert measured == {
        "/api/memory-engine/items?limit=5": 0.17,
        "/api/projects/{id}/objectives": 0.41,
        "/api/command-guard/log?limit=25": 0.34,
        "/api/system/usage": 0.51,
        "/api/dispatch/{id}": 0.40,
        "/api/dispatch/{id}/events": 0.54,
    }, measured
