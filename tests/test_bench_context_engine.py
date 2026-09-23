"""`scripts/bench_context_engine.py` runs offline against a throwaway data dir.

Run as a subprocess: the script points DATA_DIR at its own temporary dir
before importing anything, which cannot be done inside a test process that
has already imported the app with the default data dir.
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "bench_context_engine.py")


def _run(*args, timeout=240):
    env = {k: v for k, v in os.environ.items() if k != "ODYSSEUS_DATA_DIR"}
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=timeout)


def test_demo_benchmark_reports_every_query_as_json():
    done = _run("--demo", "--runs", "1", "--json")
    assert done.returncode == 0, done.stderr[-2000:]
    rows = json.loads(done.stdout)
    queries = json.load(open(os.path.join(ROOT, "docs", "evals",
                                          "context_engine_queries.json"), encoding="utf-8"))
    assert len(rows) == len(queries)
    for row in rows:
        assert row["error"] == ""
        assert row["packet_tokens"] > 0 and row["legacy_tokens"] > 0
        assert row["compile_ms"] and all(ms >= 0 for ms in row["compile_ms"])
    scored = [r for r in rows if r["recall_packet"] is not None]
    assert scored and all(r["recall_legacy"] is not None for r in scored)


def test_markdown_report_never_names_the_data_dir(tmp_path):
    out = tmp_path / "baseline.md"
    done = _run("--demo", "--runs", "1", "--write", str(out))
    assert done.returncode == 0, done.stderr[-2000:]
    text = out.read_text(encoding="utf-8")
    assert "| # | query |" in text and "## Summary" in text
    assert "p50" in text and "p95" in text
    assert "ctx-bench-demo-" not in text and str(tmp_path) not in text
