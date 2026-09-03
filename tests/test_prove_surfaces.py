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
SRC = (REPO / "static/js/workers.js").read_text(encoding="utf-8")
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


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_page_shows_a_proof_chip_beside_the_verification():
    job = dict(JOB, result=dict(JOB["result"], proof=UNPROVED))
    out = _run(f"""
      console.log(JSON.stringify({{ open: jobHtml({json.dumps(job)}, true),
        closed: jobHtml({json.dumps(job)}, false) }}));
    """)
    opened = out["open"]
    assert "<b>Proof: unproved</b>" in opened and "confidence 0.35" in opened
    assert "not a failure, not a success" in opened
    # unproved is amber, never red: it is not a failure
    assert 'class="wk-proof wk-proof-warn"' in opened
    # the top doubt is on the face of the chip, every doubt is in its title
    assert "no_verification_runner: nothing ran that could prove the work" in opened
    assert "(+1 more)" in opened
    assert "why the confidence is not 1 — no_verification_runner:" in opened
    assert "mtime_only: the folder is not a repository" in opened
    # it sits with the verification block, and a collapsed row has no chip
    assert opened.index("Not verified") < opened.index("Proof: unproved")
    assert "wk-proof" not in out["closed"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_chip_is_coloured_by_the_verdict_and_escapes_what_it_is_given():
    out = _run(f"""
      console.log(JSON.stringify({{
        proved: proofChip({json.dumps(PROVED)}),
        contradicted: proofChip({json.dumps(CONTRADICTED)}),
        partial: proofChip({{ verdict: 'partial', confidence: 0.65,
                              uncertainty: [{{ kind: 'claims_unaccounted',
                                               detail: 'w1 claims <b>x.py</b>' }}] }}),
        odd: proofChip({{ verdict: 'something new', confidence: 0.5 }}),
        none: proofChip(null), empty: proofChip({{}}) }}));
    """)
    assert 'class="wk-proof wk-proof-ok"' in out["proved"] and "confidence 1" in out["proved"]
    assert "wk-proof-why" not in out["proved"], "nothing unaccounted for = no reason line"
    assert 'class="wk-proof wk-proof-bad"' in out["contradicted"]
    assert 'class="wk-proof wk-proof-warn"' in out["partial"]
    # a worker's own words are data, never markup, wherever they land
    assert "&lt;b&gt;x.py&lt;/b&gt;" in out["partial"] and "<b>x.py</b>" not in out["partial"]
    # a verdict this page has never heard of is amber and still readable
    assert 'class="wk-proof wk-proof-warn"' in out["odd"] and "something new" in out["odd"]
    assert out["none"] == "" and out["empty"] == ""


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_job_without_a_proof_renders_exactly_as_it_did_before():
    out = _run(f"console.log(JSON.stringify({{ open: jobHtml({json.dumps(JOB)}, true) }}));")
    assert "wk-proof" not in out["open"] and "Proof:" not in out["open"]


def test_the_chip_uses_only_theme_variables():
    """No new colour is invented here: the chip reuses --ok / --warn / --red,
    the same three the verification block already borrows, each with the same
    hex fallback that block already writes."""
    import re
    css = (REPO / "static/style.css").read_text(encoding="utf-8")
    block = [line for line in css.splitlines() if line.startswith(".wk-proof")]
    assert block, "the proof chip has no styles"
    variables = set()
    for line in block:
        variables.update(re.findall(r"var\((--[a-z-]+)", line))
        # every colour literal in the rule is a var() fallback, never a raw one
        assert "#" not in re.sub(r"var\(--[a-z-]+,\s*[^)]*\)", "", line), line
    assert {"--ok", "--warn", "--red"} <= variables
    assert variables <= {"--ok", "--warn", "--red", "--fg", "--mono"}
