"""The proof has to reach the two surfaces that show a job.

`src/prove.py` answers what a dispatched job can actually SHOW — `proved`,
`partial`, `unproved` (nothing ran that could prove it: honest, not a failure)
or `contradicted` — with a confidence and a named reason for every point it is
missing. It shipped into `result.proof` and into a clause of the verdict
string, and then stopped: the MCP render a coordinator reads did not print it,
and the Workers page showed the verdict text with no chip.

So both surfaces are pinned here, and the same discipline on both: the verdict
WORD and the confidence are the answer to "may I report this as done?", the top
named doubt travels beside them, and `unproved` never renders as a failure.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_HAS_NODE = shutil.which("node") is not None

# What src/prove.py really returns for a job whose workers finished, whose
# files changed, and for which no test runner could be found.
UNPROVED = {
    "verdict": "unproved", "confidence": 0.35, "schema_version": 1, "at": 1788392400.0,
    "identity": "b3" * 32,
    "uncertainty": [
        {"kind": "no_verification_runner",
         "detail": "nothing ran that could prove the work: no test runner was detected "
                   "and no verify command was given"},
        {"kind": "mtime_only",
         "detail": "the folder is not a repository, so the changes were read from file "
                   "timestamps rather than a checkpoint diff"},
    ],
    "observations": [{"kind": "changes_observed", "detail": "2 path(s) changed on disk"}],
}
CONTRADICTED = dict(UNPROVED, verdict="contradicted", confidence=0.1, uncertainty=[
    {"kind": "verification_failed", "detail": "the verification ran and failed: 1 failed, 8 passed"},
])
PROVED = dict(UNPROVED, verdict="proved", confidence=1.0, uncertainty=[])


# ── the MCP render (mcp_servers/workers_server.py) ──────────────────────────

def _job(proof):
    return {"id": "abc123def456", "status": "done", "title": "Workers · fix total",
            "workspace": "D:/proj", "model": "qwen3.5:9b", "duration_s": 60.0,
            "verdict": "1/1 workers done · 2 files changed on disk · not verified: no runner",
            "result": {
                "workers": [{"name": "w1", "status": "done", "rounds": 3, "tool_calls": 4,
                             "failed_calls": 0, "input_tokens": 100, "output_tokens": 20,
                             "files_changed": ["cart.py"], "summary": "done"}],
                "files_changed": ["cart.py", "new.py"],
                "verification": {"mode": "auto", "ran": False, "ok": None,
                                 "summary": "no test runner detected"},
                "proof": proof,
                "totals": {"tool_calls": 4, "rounds": 3, "input_tokens": 100,
                           "output_tokens": 20, "errors": 0},
                "exit_code": 0}}


def test_the_mcp_render_prints_the_verdict_the_confidence_and_the_top_doubt(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    text = ws.render(_job(UNPROVED))
    assert "proof: unproved (confidence 0.35)" in text
    # …and says what the word means, because a coordinator reading `unproved`
    # must not round it down to "failed" or up to "done"
    assert "NOT a failure and not a success" in text
    assert ("why not certain: no_verification_runner — nothing ran that could prove the work"
            in text)
    assert "(+1 more)" in text
    # it sits with the verification, before the per-worker rows
    assert text.index("verification: not run") < text.index("proof: unproved") < text.index("[w1] done")


def test_the_mcp_render_colours_the_other_three_verdicts_by_their_meaning(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    assert "proof: contradicted (confidence 0.1) — the disk or the tests say otherwise" \
        in ws.render(_job(CONTRADICTED))
    proved = ws.render(_job(PROVED))
    assert "proof: proved (confidence 1.0)" in proved
    # nothing is unaccounted for, so there is no doubt line to print
    assert "why not certain" not in proved


def test_the_mcp_render_is_unchanged_when_there_is_no_proof(monkeypatch):
    """`agent_dispatch_prove` off — and an older job that never had one — must
    render exactly what they rendered before this block existed."""
    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    job = _job(UNPROVED)
    job["result"].pop("proof")
    assert "proof" not in ws.render(job)
    assert ws.render_proof(None) == [] and ws.render_proof({}) == []
    assert ws.render_proof({"verdict": ""}) == [] and ws.render_proof("nonsense") == []
    # a packet whose uncertainty list is junk still renders its verdict
    assert ws.render_proof({"verdict": "partial", "confidence": 0.5,
                            "uncertainty": "not a list"})[0].startswith("proof: partial")


# ── the Workers page (static/js/workers.js) ─────────────────────────────────

def _run(script: str) -> dict:
    src = (SRC.replace("export function", "function").replace("export default workersModule;", "")
           .replace("if (typeof window !== 'undefined') window.workersModule = workersModule;", ""))
    proc = subprocess.run(["node", "--input-type=module"], input=src + "\n" + script,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


JOB = {"id": "j1", "status": "done", "title": "Workers · fix total", "created": 1788392365.2,
       "duration_s": 43.8, "workspace": "D:\\proj", "model": "qwen3.5:9b", "session_id": "s1",
       "verdict": "1/1 workers done · 2 files changed on disk · not verified: no runner",
       "tasks": [],
       "result": {"workers": [], "files_changed": ["cart.py"], "totals": {"errors": 0},
                  "verification": {"mode": "auto", "ran": False, "ok": None,
                                   "summary": "no test runner detected"}}}


def test_the_screen_shows_a_proof_chip_beside_the_verification():
    """The verdict WORD and the confidence answer "may I report this as done?",
    the top named doubt travels beside them, and `unproved` never renders as a
    failure — it means nothing ran that could show it either way.
    """
    src = (REPO / "studio" / "src" / "screens" / "agents" / "Workers.tsx").read_text(encoding="utf-8")
    adapter = (REPO / "studio" / "src" / "adapters" / "workers.ts").read_text(encoding="utf-8")

    assert "res.proof.verdict" in src, "the verdict word"
    assert "res.proof.confidence" in src, "and the confidence, which is half the answer"
    assert "res.proof.uncertainty[0]" in src, "the top doubt is on the face of the chip"
    assert "why the confidence is not 1" in src, "and every doubt is in its title"

    # unproved is amber, never red: it is not a failure.
    assert "unproved: 'warn'" in adapter
    assert "contradicted: 'bad'" in adapter
    assert "proved: 'ok'" in adapter
    # An unknown verdict must still render, and amber is the honest default.
    assert "PROOF_TONE[res.proof.verdict] ?? 'warn'" in src
    # A job with no proof renders as it always did.
    assert "res.proof && res.proof.verdict &&" in src

    # Every verdict says what it means, in words, not just a colour.
    for verdict in ("proved", "partial", "unproved", "contradicted"):
        assert f"{verdict}:" in adapter.split("PROOF_WORD", 1)[1][:600]


def test_the_proof_chip_uses_only_theme_tokens():
    """No new colour is invented for the chip: it carries a tone and the
    stylesheet resolves it from the same tokens everything else uses."""
    import re
    css = (REPO / "studio" / "src" / "screens" / "agents.css").read_text(encoding="utf-8")
    block = [line for line in css.splitlines() if ".fs-wk__proof" in line or "fs-wk__proof-why" in line]
    assert block, "the proof chip has no styles"
    rules = css.split(".fs-wk__proof", 1)[1][:900]
    assert "#" not in re.sub(r"var\(--[a-z0-9-]+(?:,\s*[^)]*)?\)", "", rules), (
        "a raw colour literal in the proof chip; use a token"
    )
