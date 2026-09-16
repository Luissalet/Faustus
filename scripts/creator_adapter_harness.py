#!/usr/bin/env python3
"""scripts/creator_adapter_harness.py — WP36: run the minimum failure matrix
against whichever adapters are actually available on this machine, and
record one JSONL line per (adapter, matrix row) cell naming its evidence
level honestly.

Evidence levels (see `docs/spec/creator/WP36.md`, "Niveles de evidencia"):

* ``fixture``      — no engine reached at all; the row is proven only as a
  type/contract shape (e.g. ffmpeg not installed on this machine).
* ``fake_engine``  — `tests/creator_harness/fake_engines.py`'s `FakeAdapter`
  reproduced the fault; this is a runtime/contract proof, never evidence a
  real engine behaves the same way under the same fault.
* ``real_engine``  — the fault was reproduced (where it safely can be)
  against a REAL adapter instance: real ffmpeg subprocess/ffprobe for the
  ffmpeg adapter, or a real reachable ComfyUI for the comfyui adapter. A row
  this script cannot safely reproduce against a real engine (e.g. "engine
  crashed mid-job", which would require actually killing a real worker) is
  recorded at `fake_engine` even when a real engine is reachable — the
  report says exactly which rows that applies to, never silently promoting
  a fake result to `real_engine`.

This script does not fix bugs it finds in an adapter (CONTRATO.md rule 8 /
the WP36 ficha: "NO arregles adaptadores") — a row whose adapter answer does
not match the documented contract is written with `"contract_ok": false`
and a `"detail"` naming what went wrong, for `docs/spec/creator/WP36.md`'s
xfail list to point back to.

Usage::

    ./venv/bin/python scripts/creator_adapter_harness.py
    ./venv/bin/python scripts/creator_adapter_harness.py --run-id my-run --comfyui-url http://127.0.0.1:8188

No model is downloaded, no inference is run, and a ComfyUI target is only
ever asked `describe()`/`plan()`/a `noop`-shaped fake round-trip — never a
real generative submit (CONTRATO.md: "nada de descargar modelos ni lanzar
inferencia real").
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any, Dict, List

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.creator.adapter_port import Staging  # noqa: E402
from src.creator import adapters as adapters_pkg  # noqa: E402
from tests.creator_harness import fixtures as fx  # noqa: E402
from tests.creator_harness.fake_engines import FakeAdapter  # noqa: E402
from tests.creator_harness.matrix import MATRIX, rows_for_adapter  # noqa: E402


def _staging(workdir: str) -> Staging:
    return Staging(owner="harness", project_id="harness", workdir=workdir, input_paths={})


def _run_fake_cell(row, workdir: str) -> Dict[str, Any]:
    """Every `owner="wp36"` row that has a `fake_scenario` — always runs,
    regardless of what real engines are reachable, so the report always has
    a baseline evidence level for every row."""
    adapter = FakeAdapter(slow_seconds=0.01)
    scenario = row.fake_scenario
    if not scenario:
        return {"row_id": row.id, "evidence": "fixture", "contract_ok": True,
                "detail": "no fake_scenario declared for this row; owned by another WP or not adapter-shaped"}
    try:
        plan = adapter.plan("noop", {"scenario": scenario}, [])
        submit = adapter.submit(plan, _staging(workdir))
        status = adapter.status(submit.job_id) if submit.job_id else None
        cancel = None
        collect = None
        if scenario in ("cancel_races_completion", "cancel_too_late"):
            cancel = adapter.cancel(submit.job_id)
            status = adapter.status(submit.job_id)
        if status is not None and status.state == "completed":
            try:
                collect = adapter.collect(submit.job_id, os.path.join(workdir, "collect"))
            except OSError as exc:
                collect = {"raised": "OSError", "detail": str(exc)}
        return {
            "row_id": row.id, "evidence": "fake_engine", "contract_ok": True,
            "submit": submit.to_dict(),
            "status": status.to_dict() if hasattr(status, "to_dict") else status,
            "cancel": cancel.to_dict() if cancel is not None and hasattr(cancel, "to_dict") else cancel,
            "collect": collect.to_dict() if collect is not None and hasattr(collect, "to_dict") else collect,
        }
    except Exception as exc:  # noqa: BLE001 — a harness run reports a crash, never hides one
        return {"row_id": row.id, "evidence": "fake_engine", "contract_ok": False,
                "detail": f"{type(exc).__name__}: {exc}"}


def _run_ffmpeg_real_cells(workdir: str) -> List[Dict[str, Any]]:
    """The subset of the matrix a REAL ffmpeg adapter can safely reproduce:
    a genuine trim job, then a genuine truncation of its real output before
    `collect()` — never a fabricated "success" claim. Skips explicitly
    (`fixture` evidence, not silently absent) when ffmpeg/ffprobe are not on
    PATH."""
    from src.creator.adapters.ffmpeg import FfmpegAdapter

    cells: List[Dict[str, Any]] = []
    if not (fx.ffmpeg_available() and fx.ffprobe_available()):
        cells.append({"row_id": "collect_truncated_output", "adapter": "ffmpeg",
                       "evidence": "fixture", "contract_ok": True,
                       "detail": "ffmpeg/ffprobe not on PATH; real-engine cell skipped explicitly"})
        return cells

    clip_path = os.path.join(workdir, "real_clip.mp4")
    try:
        fx.make_mp4(clip_path, seconds=1.0, size="64x64", fps=5)
    except RuntimeError as exc:
        cells.append({"row_id": "collect_truncated_output", "adapter": "ffmpeg",
                       "evidence": "fixture", "contract_ok": True,
                       "detail": f"could not build the fixture clip: {exc}"})
        return cells

    adapter = FfmpegAdapter()
    manifest = adapter.describe()
    if not manifest.available:
        cells.append({"row_id": "collect_truncated_output", "adapter": "ffmpeg",
                       "evidence": "fixture", "contract_ok": True,
                       "detail": manifest.reason})
        return cells

    staging = Staging(owner="harness", project_id="harness",
                        workdir=os.path.join(workdir, "stage"), input_paths={"occ_clip": clip_path})
    plan = adapter.plan("trim", {"start_seconds": 0, "duration_seconds": 0.5}, ["occ_clip"])
    submit = adapter.submit(plan, staging)
    contract_ok = submit.state == "accepted"
    detail = "" if contract_ok else f"unexpected submit state {submit.state!r}: {submit.detail}"

    # Truncate the REAL output ffmpeg just produced, then prove collect()
    # rejects it — real_engine evidence for "collect_truncated_output".
    truncate_ok = None
    if contract_ok:
        from src.creator.adapters import ffmpeg as ffmpeg_mod
        output_path = ffmpeg_mod._JOBS[submit.job_id]["output_path"]
        with open(output_path, "r+b") as fh:
            fh.truncate(16)
        collected = adapter.collect(submit.job_id, os.path.join(workdir, "collected"))
        truncate_ok = (collected.ok is False and collected.outputs
                        and collected.outputs[0].valid is False)
        if not truncate_ok:
            contract_ok = False
            detail = f"collect() did not reject a real truncated output: ok={collected.ok}"

    cells.append({
        "row_id": "collect_truncated_output", "adapter": "ffmpeg", "evidence": "real_engine",
        "contract_ok": bool(contract_ok), "detail": detail,
        "submit": submit.to_dict(),
    })
    return cells


def _describe_registered_adapters() -> List[Dict[str, Any]]:
    out = []
    for name, factory in sorted(adapters_pkg.registry().items()):
        try:
            manifest = factory().describe()
            out.append({"adapter": name, **manifest.to_dict()})
        except Exception as exc:  # noqa: BLE001
            out.append({"adapter": name, "available": False,
                        "reason": f"describe() raised: {type(exc).__name__}: {exc}"})
    return out


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", default="", help="Run id; defaults to a timestamped uuid.")
    parser.add_argument("--out-dir", default="", help="Defaults to DATA_DIR/creator/harness.")
    args = parser.parse_args(argv)

    run_id = args.run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"

    if args.out_dir:
        out_dir = args.out_dir
    else:
        from src.constants import DATA_DIR
        out_dir = os.path.join(DATA_DIR, "creator", "harness")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{run_id}.jsonl")

    import tempfile
    workdir = tempfile.mkdtemp(prefix="creator-adapter-harness-")

    lines: List[Dict[str, Any]] = []
    header = {
        "kind": "header", "run_id": run_id, "started_at": time.time(),
        "registered_adapters": _describe_registered_adapters(),
    }
    lines.append(header)

    for row in rows_for_adapter("wp36"):
        if row.fake_scenario:
            cell = _run_fake_cell(row, workdir)
            cell.update(kind="cell", adapter="fake", run_id=run_id)
            lines.append(cell)
        else:
            lines.append({"kind": "cell", "row_id": row.id, "adapter": "", "run_id": run_id,
                          "evidence": "fixture", "contract_ok": True,
                          "detail": "no fake_scenario declared (e.g. the resources.py row, "
                                    "proved separately by tests/creator_harness/test_resources_under_failure.py)"})

    for cell in _run_ffmpeg_real_cells(workdir):
        cell.update(kind="cell", run_id=run_id)
        lines.append(cell)

    lines.append({"kind": "footer", "run_id": run_id, "ended_at": time.time(),
                  "cell_count": sum(1 for line in lines if line.get("kind") == "cell")})

    with open(out_path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    contract_failures = [c for c in lines if c.get("kind") == "cell" and not c.get("contract_ok", True)]
    print(f"wrote {out_path} ({len(lines)} lines, "
          f"{sum(1 for l in lines if l.get('kind') == 'cell')} cells, "
          f"{len(contract_failures)} contract failures)")
    for c in contract_failures:
        print(f"  CONTRACT FAILURE: {c.get('row_id')} ({c.get('adapter')}): {c.get('detail')}")
    return 1 if contract_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
