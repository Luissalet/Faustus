"""The Agent runners page (static/js/agentRunners.js): one row per CLI agent
this machine could run as a worker, over /api/agent-runners.

The renderers are pure (kept in a marked, dependency-free region) and run in
node; the wiring is pinned at source level, like the Experts page.

Three of these are the feature's honesty rules, not cosmetics:

  * the LICENCE WORD is printed verbatim, and a word the backend does not
    vouch for renders as "unknown" — never as a guess derived from the name;
  * "installed" and "can be a worker" are two facts and stay two: a GUI that is
    installed is still not a worker, and an agent with no recorded invocation
    says exactly that;
  * the sentence the whole feature turns on — Faustus's command guard cannot
    see inside another agent's own shell — is on the page, above the table.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / "static/js/agentRunners.js").read_text(encoding="utf-8")
CSS = (REPO / "static/style.css").read_text(encoding="utf-8")
INDEX = (REPO / "static/index.html").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

PURE_START = "// ── Agent runners: pure helpers"
PURE_END = "// ── Agent runners: end pure helpers ──"

GUARD = ("an external agent runs its own shell: Faustus's command guard does not see the "
         "commands it runs, only what changed on disk afterwards")

PAYLOAD = {
    "enabled": False,
    "guard_note": GUARD,
    "timeout_s": 900,
    "runners": [
        {"key": "claude", "label": "Claude <Code>", "aliases": [], "kind": "cli",
         "licence": "subscription", "install": "ollama launch claude",
         "launch_command": "ollama launch claude -y", "argv": ["claude", "-p", "{task}"],
         "installed": True, "path": "/usr/local/bin/claude", "invocation_known": True,
         "runnable_as_worker": True, "notes": "Print mode runs one prompt and exits."},
        {"key": "vscode", "label": "VS Code", "aliases": ["code"], "kind": "app",
         "licence": "unknown", "install": "ollama launch vscode",
         "launch_command": "ollama launch vscode -y", "argv": [],
         "installed": True, "path": "/usr/local/bin/code", "invocation_known": False,
         "runnable_as_worker": False, "notes": "An editor: the licence word is unknown."},
        {"key": "opencode", "label": "OpenCode", "aliases": [], "kind": "cli",
         "licence": "open", "install": "ollama launch opencode",
         "launch_command": "ollama launch opencode -y", "argv": ["opencode", "run", "{task}"],
         "installed": False, "invocation_known": True, "runnable_as_worker": False,
         "notes": "Its documented headless form."},
        {"key": "brandnew", "label": "Brand New Agent", "aliases": ["bna"], "kind": "cli",
         "licence": "unknown", "install": "ollama launch brandnew",
         "launch_command": "ollama launch brandnew -y", "argv": [],
         "installed": False, "invocation_known": False, "runnable_as_worker": False,
         "notes": "known to Ollama, not runnable as a worker yet"},
    ],
}


def _pure() -> str:
    """The dependency-free helper region: no DOM, no imports, runs in node."""
    assert PURE_START in SRC and PURE_END in SRC, "pure-helper markers missing from agentRunners.js"
    region = SRC.split(PURE_START, 1)[1].split(PURE_END, 1)[0]
    return region.split("\n", 1)[1]


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "--input-type=module"], input=_pure() + "\n" + script,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_parses_and_is_wired():
    assert subprocess.run(["node", "--check", str(REPO / "static/js/agentRunners.js")],
                          capture_output=True).returncode == 0
    assert "onclick=" not in SRC and "alert(" not in SRC and "window.confirm(" not in SRC
    assert "/api/agent-runners" in SRC
    # the launch is a POST the human pressed, and it streams
    assert "method: 'POST'" in SRC and "/launch`" in SRC and "getReader()" in SRC
    for entry in ("export async function openRunnersPanel", "export function closeRunnersPanel",
                  "export async function loadRunners"):
        assert entry in SRC, entry
    for fn in ("runnersPageHtml", "runnerRowHtml", "normalizeCatalogue", "normalizeRunner",
               "sortRunners", "filterRunners", "licenceWord", "licenceHint", "workerStatus",
               "launchLogLine"):
        assert f"function {fn}" in SRC, fn
    # delegated listeners on the modal, not per-row handlers
    assert "modal.addEventListener('click'" in SRC and "modal.addEventListener('input'" in SRC
    assert "data-run-copy" in SRC and "data-run-launch" in SRC and "data-run-refresh" in SRC
    # errors land inline, not in a dialog
    assert "data-run-error" in SRC and "function inlineError" in SRC
    # the honesty rules are in the source, not only in the rendered output
    assert "licenceWord(row.licence)" in SRC and "It never invents a word" in SRC


def test_pure_region_is_actually_pure():
    pure = _pure()
    for forbidden in ("document.", "window.", "fetch(", "navigator.", "$("):
        assert forbidden not in pure, forbidden


def test_the_page_has_an_entry_point_and_a_modal_shell():
    assert 'id="tool-agent-runners-btn"' in INDEX and ">Agent runners</span>" in INDEX
    assert 'id="agent-runners-modal"' in INDEX and 'id="agent-runners-body"' in INDEX
    assert 'aria-label="Close agent runners"' in INDEX and 'id="close-agent-runners-modal"' in INDEX
    assert "/static/js/agentRunners.js" in INDEX


def test_a_clearly_delimited_css_block_uses_theme_tokens_only():
    assert "/* ── Agent runners ──" in CSS
    for selector in (".run-table", ".run-licence.is-unknown", ".run-licence.is-open",
                     ".run-licence.is-subscription", ".run-installed.is-yes", ".run-worker.is-yes",
                     ".run-guard", ".run-log", ".run-empty"):
        assert selector in CSS, selector
    block = CSS.split("/* ── Agent runners ──", 1)[1]
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b(?![\w-])", block)
    assert not hexes, f"hardcoded hex colour in the Agent runners block: {hexes}"
    # no "#" at all: id selectors would trip the guard the Experts block set
    assert "#" not in block.split("*/", 1)[1], "the Agent runners block must be class-only"


# ── the table ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_table_prints_the_licence_word_and_keeps_the_two_facts_apart():
    out = _run(f"""
      const P = {json.dumps(PAYLOAD)};
      console.log(JSON.stringify({{
        html: runnersPageHtml(P, {{}}),
        on: runnersPageHtml({{ ...P, enabled: true }}, {{}}),
        loading: runnersPageHtml(null, {{ loading: true }}),
        empty: runnersPageHtml({{ runners: [] }}, {{}}),
        error: runnersPageHtml(P, {{ error: 'HTTP 500 <x>' }}),
        nomatch: runnersPageHtml(P, {{ query: 'zzz' }}),
      }}));
    """)
    html = out["html"]
    # the licence word, verbatim, one per row
    assert ">subscription<" in html and ">open<" in html and html.count(">unknown<") == 2
    assert "run-licence is-unknown" in html
    # installed vs "can be a worker" are separate columns and separate words
    assert html.count('class="run-installed is-yes">installed<') == 2
    assert html.count('class="run-installed">not installed<') == 2
    assert "can be a worker" in html and "GUI, never a worker" in html
    assert "no invocation recorded" in html
    # only ONE agent is usable right now (claude): vscode is a GUI, opencode
    # is not installed, brandnew has no invocation
    assert html.count("run-worker is-yes") == 1
    assert "1 usable as a worker right now" in html and "4 known" in html and "2 installed" in html
    # the launch command with a copy button, per row
    assert html.count('data-run-copy="ollama launch') == 4
    assert 'data-run-launch="claude"' in html and "onclick=" not in html
    # the dispatch field is named for the one that can be a worker
    assert "&quot;runner&quot;: &quot;claude&quot;" in html
    # the label is escaped
    assert "Claude &lt;Code&gt;" in html and "<Code>" not in html
    # the sentence that must never be hidden
    assert "command guard does not see" in html and "external_agent_unguarded" in html
    # the setting is reported both ways
    assert "agent_external_runners" in html and "<strong>off</strong>" in html
    assert "<strong>on</strong>" in out["on"]
    # states
    assert "Loading agent runners" in out["loading"]
    assert "No agent runners" in out["empty"]
    assert "HTTP 500 &lt;x&gt;" in out["error"] and "<x>" not in out["error"]
    assert "data-run-error hidden>" in html and "data-run-error hidden>" not in out["error"]
    assert "No agent matches that search." in out["nomatch"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_licence_word_is_never_invented():
    out = _run("""
      console.log(JSON.stringify({
        good: ['open', 'subscription', 'unknown', ' OPEN '].map(licenceWord),
        junk: [null, undefined, '', 'free', 'MIT', 'apache-2.0', 42, {}, []].map(licenceWord),
        row: runnerRowHtml({ key: 'x', label: 'X', licence: 'MIT', kind: 'cli' }),
      }));
    """)
    assert out["good"] == ["open", "subscription", "unknown", "open"]
    # a word the backend does not vouch for is "unknown" — never repeated as a
    # claim, and never guessed from the name
    assert out["junk"] == ["unknown"] * 9
    assert ">unknown<" in out["row"] and "MIT" not in out["row"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_worker_status_says_which_of_the_two_facts_is_missing():
    out = _run(f"""
      const rows = {json.dumps(PAYLOAD["runners"])};
      console.log(JSON.stringify(rows.map(workerStatus).concat([
        workerStatus(null), workerStatus({{ key: 'k', kind: 'cli', argv: ['k'], installed: true }}),
      ])));
    """)
    claude, vscode, opencode, brandnew, junk, bare = out
    assert claude["can"] is True and 'runner": "claude"' in claude["detail"]
    assert vscode["can"] is False and vscode["label"] == "GUI, never a worker"
    assert opencode["can"] is False and opencode["label"] == "not installed"
    assert "ollama launch opencode" in opencode["detail"]
    assert brandnew["can"] is False and brandnew["label"] == "no invocation recorded"
    assert junk["can"] is False           # junk in, a refusal out, never a crash
    assert bare["can"] is True


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_order_puts_what_this_machine_can_use_first():
    out = _run(f"""
      const P = {json.dumps(PAYLOAD)};
      console.log(JSON.stringify({{
        order: sortRunners(normalizeCatalogue(P)).map(r => r.key),
        find: filterRunners(normalizeCatalogue(P), 'CODE').map(r => r.key),
        byAlias: filterRunners(normalizeCatalogue(P), 'bna').map(r => r.key),
        all: filterRunners(normalizeCatalogue(P), '   ').map(r => r.key),
        none: filterRunners(normalizeCatalogue(P), 'nothing').length,
        wrapped: normalizeCatalogue({{ data: {{ runners: P.runners }} }}).map(r => r.key),
        bare: normalizeCatalogue(P.runners).map(r => r.key),
        junk: normalizeCatalogue(null).length + normalizeCatalogue(7).length + normalizeCatalogue({{}}).length,
      }}));
    """)
    # installed first, then the ones that know how to be a worker
    assert out["order"] == ["claude", "vscode", "opencode", "brandnew"]
    assert out["find"] == ["claude", "vscode", "opencode"]   # label, key and alias all match
    assert out["byAlias"] == ["brandnew"]
    assert out["all"] == ["claude", "vscode", "opencode", "brandnew"]
    assert out["none"] == 0
    assert out["wrapped"] == out["bare"] == ["claude", "vscode", "opencode", "brandnew"]
    assert out["junk"] == 0


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_launch_log_reports_the_end_honestly():
    out = _run("""
      console.log(JSON.stringify([
        launchLogLine({ event: 'started', command: 'ollama launch qwen -y' }),
        launchLogLine({ event: 'output', line: 'downloading…' }),
        launchLogLine({ event: 'error', message: 'ollama is not installed' }),
        launchLogLine({ event: 'end', exit_code: 0, installed: true }),
        launchLogLine({ event: 'end', exit_code: 1, installed: false }),
        launchLogLine({ event: 'end' }),
        launchLogLine(null),
      ]));
    """)
    assert out[0] == "$ ollama launch qwen -y"
    assert out[1] == "downloading…"
    assert out[2].startswith("error: ")
    assert out[3] == "— finished (exit 0); it is now installed"
    # a launch that exits 0 but installed nothing does NOT get to claim success
    assert out[4] == "— finished (exit 1); it is still not installed"
    assert out[5] == "— finished (exit unknown); it is still not installed"
    assert out[6] == ""
