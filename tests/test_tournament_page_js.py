"""The Tournament page (static/js/tournament.js): the setup form, the per-model
board and the ranked results table over /api/tournament/*.

The renderers are pure (kept in a marked, dependency-free region of
tournament.js) and run in node; the wiring is pinned at source level, like the
Experts and Learned-rules pages.

Two of these are the feature's honesty rules, not cosmetics:

  * a score the judge did not give renders as an em dash carrying the reason —
    never as a number and never as a zero, because a plausible-looking 0 is
    indistinguishable from a judgement of 0; and
  * a ranking with no judge behind it says "no judge available" next to the
    table, so a deterministic tiebreak is never read as a verdict.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / "static/js/tournament.js").read_text(encoding="utf-8")
CSS = (REPO / "static/style.css").read_text(encoding="utf-8")
INDEX = (REPO / "static/index.html").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

PURE_START = "// ── Tournament: pure helpers"
PURE_END = "// ── Tournament: end pure helpers ──"

RUN = {
    "id": "abc123abc123", "status": "done", "prompt": "write a <b>CSV</b> parser",
    "models": ["qwen3.5:9b", "llama3:8b"], "rounds": 3, "judge_model": "qwen3.5:9b",
    "duration_s": 12.34,
    "result": {
        "rounds_run": 2, "stopped_by": "convergence",
        "convergence": {"score": 0.812, "converged": True, "reason": "every model settled <ok>"},
        "answers": [
            {"entry": 0, "model": "qwen3.5:9b", "round": 0, "text": "A <b>first</b> draft",
             "elapsed_s": 1.25, "tokens": 10, "tokens_source": "reported"},
            {"entry": 0, "model": "qwen3.5:9b", "round": 1, "text": "A hybrid draft",
             "elapsed_s": 1.5, "tokens": 12, "tokens_source": "estimated"},
            {"entry": 1, "model": "llama3:8b", "round": 0, "text": "B first draft",
             "elapsed_s": 2.0, "tokens": 8, "tokens_source": "reported"},
        ],
        "final": [
            {"entry": 0, "model": "qwen3.5:9b", "round": 1, "text": "A hybrid draft",
             "outcome": "success",
             "scores": {"correctness": 90, "completeness": 80, "sophistication": 70},
             "total": 240, "tiebreak": 0.713, "rank": 1, "note": "solid"},
            {"entry": 1, "model": "llama3:8b", "round": 0, "text": "B first draft",
             "outcome": "error", "scores": None, "total": None, "tiebreak": 0.421, "rank": 2},
        ],
        "judge": {"model": "qwen3.5:9b", "ok": True, "attempts": 1, "error": None},
        "ranking": "mixed", "ranking_note": "the judge scored 1 of 2 answers",
        "merge_prompt": "", "errors": [{"entry": 1, "model": "llama3:8b", "round": 1,
                                        "error": "the endpoint <died>"}],
        "cancelled": [], "degraded": True,
    },
}

MODELS_PAYLOAD = {"items": [
    {"endpoint_name": "local", "models": ["qwen3.5:9b", "llama3:8b"],
     "models_display": ["ollama/qwen3.5:9b", "ollama/llama3:8b"],
     "models_extra": ["mistral:7b"], "models_extra_display": []},
    {"endpoint_name": "local", "models": ["qwen3.5:9b"], "models_display": []},
    None,
]}


def _pure() -> str:
    """The dependency-free helper region: no DOM, no imports, runs in node."""
    assert PURE_START in SRC and PURE_END in SRC, "pure-helper markers missing from tournament.js"
    region = SRC.split(PURE_START, 1)[1].split(PURE_END, 1)[0]
    return region.split("\n", 1)[1]  # drop the tail of the marker comment line


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "--input-type=module"], input=_pure() + "\n" + script,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_module_parses_and_is_wired():
    assert subprocess.run(["node", "--check", str(REPO / "static/js/tournament.js")],
                          capture_output=True).returncode == 0
    # no inline handlers, no native dialogs
    assert "onclick=" not in SRC and "alert(" not in SRC and "window.confirm(" not in SRC
    # every endpoint this page needs, all under the one request helper
    assert "/api/tournament" in SRC and "/api/models" in SRC
    for path in ("'?limit=20'", "/cancel`", "method: 'POST'"):
        assert path in SRC, path
    # exported entry points + the pure helpers the tests below drive
    for entry in ("export async function openTournamentPanel",
                  "export function closeTournamentPanel",
                  "export function initTournament",
                  "export const startTournament", "export const cancelTournament"):
        assert entry in SRC, entry
    for fn in ("setupHtml", "boardHtml", "resultsTableHtml", "modelCardHtml",
               "modelPickerHtml", "judgePickerHtml", "runListHtml", "scoreCellHtml",
               "mergePromptFor", "normalizeRun", "normalizeFinal", "normalizeAnswer",
               "modelRowsFrom", "rankingLabel", "stoppedByLabel", "answersByEntry",
               "winnerOf", "isLiveStatus"):
        assert f"function {fn}" in SRC, fn
    # delegated listeners on the modal, not per-row handlers
    assert "modal.addEventListener('click'" in SRC and "modal.addEventListener('submit'" in SRC
    assert "modal.addEventListener('change'" in SRC
    # errors land inline, not in a dialog — and are actually used, not just declared
    assert "data-trn-error" in SRC and "function inlineError" in SRC
    assert SRC.count("inlineError(") > 2
    # polling stops when the run does, and when the panel closes
    assert "function stopPolling" in SRC and "schedulePoll()" in SRC
    assert "stopPolling();" in SRC.split("export function closeTournamentPanel", 1)[1]
    # the Merge button assembles the synthesis prompt and drops it in the composer
    assert "function mergeIntoComposer" in SRC and "data-trn-merge" in SRC
    assert "$('message')" in SRC and "new Event('input'" in SRC
    # it does NOT send for the user
    assert "requestSubmit" not in SRC and "chat-form" not in SRC


def test_pure_region_is_actually_pure():
    pure = _pure()
    for forbidden in ("document.", "window.", "fetch(", "uiModule", "$("):
        assert forbidden not in pure, forbidden


def test_the_page_has_an_entry_point_and_a_modal_shell():
    assert 'id="tool-tournament-btn"' in INDEX and ">Tournament</span>" in INDEX
    assert 'id="tournament-modal"' in INDEX
    for slot in ("tournament-setup", "tournament-board"):
        assert f'id="{slot}"' in INDEX, slot
    assert 'aria-label="Close tournament"' in INDEX and 'id="close-tournament-modal"' in INDEX
    assert "/static/js/tournament.js" in INDEX
    # it sits beside the comparator, it does not replace it
    assert "compare" in INDEX.lower()


def test_a_clearly_delimited_css_block_uses_theme_tokens_only():
    assert "/* ── Tournament ──" in CSS
    for selector in (".tournament-page", ".trn-setup", ".trn-model.is-on", ".trn-card",
                     ".trn-round-chip.is-on", ".trn-answer", ".trn-table",
                     ".trn-result-row.is-winner", ".trn-score.is-null",
                     ".trn-ranking.is-deterministic", ".trn-stopped.is-converged",
                     ".trn-merge-btn"):
        assert selector in CSS, selector
    block = CSS.split("/* ── Tournament ──", 1)[1]
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b(?![\w-])", block)
    assert not hexes, f"hardcoded hex colour in the Tournament block: {hexes}"
    # No "#" at all, in fact: an id selector here would also trip the guard the
    # Learned-rules and Experts blocks put on everything that follows them.
    assert "#" not in block.split("*/", 1)[1], "the Tournament block must be class-only"
    # every button that paints itself owns its hover (the global button:hover
    # would repaint it panel-coloured under the cursor)
    for btn in (".trn-btn", ".trn-run-btn", ".trn-cancel-btn", ".trn-back-btn",
                ".trn-merge-btn"):
        assert f"{btn}:hover" in block, btn


# ── the setup form ─────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_setup_form_picks_models_within_the_cap_and_gates_the_run_button():
    rows = [{"id": "a:9b", "name": "a:9b", "endpoint": "local"},
            {"id": "b:8b", "name": "b:8b", "endpoint": ""},
            {"id": "c:7b", "name": "c:7b", "endpoint": "remote"}]
    out = _run(f"""
      const ROWS = {json.dumps(rows)};
      console.log(JSON.stringify({{
        empty: setupHtml({{ models: ROWS, selected: [], prompt: '' }}),
        one: setupHtml({{ models: ROWS, selected: ['a:9b'], prompt: 'do <b>it</b>' }}),
        ready: setupHtml({{ models: ROWS, selected: ['a:9b','b:8b'], prompt: 'go', judge: 'b:8b' }}),
        full: setupHtml({{ models: ROWS, selected: ['a:9b','b:8b'], prompt: 'go', max_models: 2 }}),
        off: setupHtml({{ models: ROWS, selected: [], prompt: '', enabled: false }}),
        error: setupHtml({{ models: ROWS, selected: [], prompt: '', error: 'HTTP 500 <x>' }}),
        noModels: setupHtml({{ models: [], selected: [] }}),
        starting: setupHtml({{ models: ROWS, selected: ['a:9b','b:8b'], prompt: 'go', starting: true }}),
      }}));
    """)
    # the prompt is escaped, never re-parsed as markup
    assert "do &lt;b&gt;it&lt;/b&gt;" in out["one"] and "<b>it</b>" not in out["one"]
    # one checkbox per model, by data attribute, no inline handlers
    assert out["one"].count('data-trn-model="') == 3 and "onclick=" not in out["one"]
    assert 'data-trn-model="a:9b" checked' in out["one"]
    assert "1 of 4 picked · pick at least 2" in out["one"]
    # the run button only opens once there are two models AND a prompt
    assert "data-trn-run disabled" in out["empty"] and "data-trn-run disabled" in out["one"]
    assert "data-trn-run>" in out["ready"]
    assert "Starting…" in out["starting"] and "data-trn-run disabled" in out["starting"]
    # at the cap the unpicked ones are visibly closed, not hidden
    assert out["full"].count("is-full") == 1 and "disabled>" in out["full"]
    assert "2 of 2 picked" in out["full"]
    # the judge picker only offers the entrants, plus the default
    assert "strongest of the entrants" in out["ready"]
    assert '<option value="b:8b" selected>' in out["ready"]
    assert 'value="c:7b"' not in out["ready"].split("data-trn-judge", 1)[1].split("</select>", 1)[0]
    # states
    assert "switched off in" in out["off"] and "switched off" not in out["ready"]
    assert "HTTP 500 &lt;x&gt;" in out["error"] and "<x>" not in out["error"]
    assert "data-trn-error>" in out["error"] and 'data-trn-error>' in out["empty"]
    assert " hidden data-trn-error" in out["empty"] and " hidden data-trn-error" not in out["error"]
    assert "No models found" in out["noModels"]
    # the rounds field says what it is: a maximum
    assert "Rounds (max)" in out["ready"] and "stops by itself" in out["ready"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_earlier_runs_are_listed_and_open_by_data_attribute():
    runs = [{"id": "r1", "prompt": "an <old> task", "models": ["a:9b", "b:8b"], "status": "done"},
            {"id": "r2", "prompt": "", "models": [], "status": "cancelled"}]
    out = _run(f"""
      console.log(JSON.stringify({{
        html: runListHtml({json.dumps(runs)}),
        none: runListHtml([]),
        junk: runListHtml(null) + runListHtml([null, 'x']),
      }}));
    """)
    assert 'data-trn-open="r1"' in out["html"] and 'data-trn-open="r2"' in out["html"]
    assert "an &lt;old&gt; task" in out["html"] and "<old>" not in out["html"]
    assert "(no prompt)" in out["html"]
    assert "trn-state-done" in out["html"] and "trn-state-cancelled" in out["html"]
    assert out["none"] == ""
    assert "trn-past-list" in out["junk"] and "data-trn-open=\"\"" in out["junk"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_model_list_is_flattened_and_deduped():
    out = _run(f"""
      console.log(JSON.stringify({{
        rows: modelRowsFrom({json.dumps(MODELS_PAYLOAD)}),
        junk: modelRowsFrom(null),
        empty: modelRowsFrom({{ items: [] }}),
        odd: modelRowsFrom({{ items: [{{ models: 'not a list' }}, {{ models: [null, 'ok:1b'] }}] }}),
      }}));
    """)
    assert [r["id"] for r in out["rows"]] == ["qwen3.5:9b", "llama3:8b", "mistral:7b"]
    assert out["rows"][0]["name"] == "qwen3.5:9b"      # the display name loses its vendor prefix
    assert out["rows"][0]["endpoint"] == "local"
    assert out["rows"][2]["name"] == "mistral:7b"      # no display list: the id is the name
    assert out["junk"] == [] and out["empty"] == []
    assert [r["id"] for r in out["odd"]] == ["ok:1b"]


# ── the board ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_each_model_gets_a_card_that_fills_in_round_by_round():
    out = _run(f"""
      const R = normalizeRun({json.dumps(RUN)});
      const LIVE = normalizeRun({{ ...{json.dumps(RUN)}, status: 'running',
        progress: [{{ entry: 0, model: 'qwen3.5:9b', state: 'running', round: 1 }},
                   {{ entry: 1, model: 'llama3:8b', state: 'queued', round: null }}] }});
      console.log(JSON.stringify({{
        html: boardHtml(R, {{}}),
        round0: boardHtml(R, {{ round: 0 }}),
        live: boardHtml(LIVE, {{}}),
        nothing: boardHtml(null, {{}}),
      }}));
    """)
    html = out["html"]
    # one card per model, opened by data attribute
    assert html.count('data-trn-card="') == 2
    assert ">qwen3.5:9b<" in html and ">llama3:8b<" in html
    # round chips: the blind round is named as such
    assert 'data-trn-round="0" data-trn-entry="0"' in html and ">blind<" in html
    assert 'data-trn-round="1" data-trn-entry="0"' in html and ">round 1<" in html
    # the latest answer is shown by default, escaped, with its own footer
    assert "A hybrid draft" in html
    assert "A &lt;b&gt;first&lt;/b&gt; draft" in out["round0"]
    assert "<b>first</b>" not in out["round0"]
    assert "14 chars · 1.5s · 12 tokens (estimated)" in html
    assert "(estimated)" not in out["round0"], "a reported count is not labelled an estimate"
    # a model that failed says why, on its own card
    assert "the endpoint &lt;died&gt;" in html and "trn-card is-error" in html
    # the header: status, the rounds actually run, and why they stopped
    assert "2 of 3 rounds · 12.3s" in html
    assert "stopped early: the rounds converged (0.81)" in html
    assert "trn-stopped is-converged" in html
    # a finished run offers a new tournament; a running one offers Stop
    assert "data-trn-back" in html and "data-trn-cancel" not in html
    assert "data-trn-cancel" in out["live"] and "data-trn-back" not in out["live"]
    assert "trn-state-running" in out["live"] and "waiting for this model" not in html
    assert "No tournament loaded." in out["nothing"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_model_with_no_answer_yet_waits_instead_of_showing_an_empty_box():
    out = _run("""
      const R = normalizeRun({ id: 'x', status: 'running', models: ['a:9b', 'b:8b'],
        rounds: 3, result: { rounds_run: 0, answers: [], final: [] } });
      console.log(JSON.stringify({ html: boardHtml(R, {}), table: resultsTableHtml(R) }));
    """)
    assert out["html"].count("waiting for this model…") == 2
    assert "trn-card is-queued" in out["html"]
    assert out["table"] == "", "no finalists, no table"


# ── the results table ──────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_the_table_shows_the_three_scores_the_rank_and_the_winner():
    out = _run(f"""
      const R = normalizeRun({json.dumps(RUN)});
      console.log(JSON.stringify({{ html: resultsTableHtml(R) }}));
    """)
    html = out["html"]
    assert "<th>Correct</th><th>Complete</th><th>Sophist.</th>" in html
    assert '<td class="trn-score">90</td>' in html
    assert '<td class="trn-score">80</td>' in html
    assert '<td class="trn-score">70</td>' in html
    assert '<td class="trn-total">240</td>' in html
    assert '<td class="trn-tiebreak">0.713</td>' in html
    # the winner is highlighted and marked
    assert 'class="trn-result-row is-winner is-success"' in html
    assert html.count("trn-crown") == 1
    # the failed entrant is still in the table, labelled, not silently dropped
    assert '<span class="trn-outcome">error</span>' in html
    assert "data-trn-merge" in html and "onclick=" not in html
    assert "every model settled &lt;ok&gt;" in html and "<ok>" not in html
    assert "judge: qwen3.5:9b" in html


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_score_the_judge_did_not_give_is_never_a_number():
    """The honesty rule: an em dash with the reason on it, not a plausible 0."""
    out = _run("""
      console.log(JSON.stringify({
        missing: scoreCellHtml(null) + scoreCellHtml(undefined) + scoreCellHtml('')
                 + scoreCellHtml('nope') + scoreCellHtml(NaN),
        zero: scoreCellHtml(0),
        real: scoreCellHtml(90) + scoreCellHtml('75'),
        row: resultsTableHtml(normalizeRun({ id: 'x', status: 'done', models: ['a:9b'],
          result: { ranking: 'deterministic',
                    ranking_note: 'no judge available — ranked by a deterministic tiebreak',
                    final: [{ entry: 0, model: 'a:9b', text: 'x', scores: null, total: null,
                              tiebreak: 0.5, rank: 1 }] } })),
      }));
    """)
    assert out["missing"].count("is-null") == 5
    assert "—" in out["missing"] and "0<" not in out["missing"]
    assert "the judge did not score this" in out["missing"]
    assert '<td class="trn-score">0</td>' in out["zero"], "a real zero IS a score"
    assert "is-null" not in out["zero"]
    assert '<td class="trn-score">90</td>' in out["real"] and '>75<' in out["real"]
    # and the whole table falls back without inventing anything
    assert out["row"].count("is-null") == 3 and '<td class="trn-total">—</td>' in out["row"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_a_ranking_with_no_judge_behind_it_says_so():
    out = _run("""
      const base = { id: 'x', status: 'done', models: ['a:9b'],
        result: { final: [{ entry: 0, model: 'a:9b', text: 'x', tiebreak: 0.5, rank: 1 }] } };
      const of = (extra) => normalizeRun({ ...base, result: { ...base.result, ...extra } });
      console.log(JSON.stringify({
        judged: rankingLabel(of({ ranking: 'judge' })),
        none: rankingLabel(of({ ranking: 'deterministic',
                                ranking_note: 'no judge available — ranked by a tiebreak' })),
        bare: rankingLabel(of({ ranking: 'deterministic' })),
        mixed: rankingLabel(of({ ranking: 'mixed', ranking_note: 'the judge scored 1 of 2' })),
        junk: rankingLabel(null),
        html: resultsTableHtml(of({ ranking: 'deterministic',
                                    ranking_note: 'no judge available — <x> ranked by a tiebreak' })),
      }));
    """)
    assert out["judged"] == {"kind": "judge", "text": "ranked by the judge"}
    assert out["none"]["kind"] == "deterministic" and "no judge available" in out["none"]["text"]
    assert "no judge available" in out["bare"]["text"], "the label never depends on the server's note"
    assert out["mixed"]["kind"] == "mixed" and "1 of 2" in out["mixed"]["text"]
    assert out["junk"]["kind"] == "deterministic"
    assert 'class="trn-ranking is-deterministic"' in out["html"]
    assert "no judge available — &lt;x&gt;" in out["html"] and "<x>" not in out["html"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_what_ended_the_rounds_is_said_in_words():
    out = _run("""
      const of = (extra) => normalizeRun({ id: 'x', status: 'done', models: ['a'],
        rounds: 4, result: { rounds_run: 2, final: [], ...extra } });
      console.log(JSON.stringify({
        converged: stoppedByLabel(of({ stopped_by: 'convergence',
                                       convergence: { score: 0.9123 } })),
        noScore: stoppedByLabel(of({ stopped_by: 'convergence' })),
        rounds: stoppedByLabel(of({ stopped_by: 'rounds' })),
        one: stoppedByLabel(of({ stopped_by: 'rounds', rounds_run: 1 })),
        cancelled: stoppedByLabel(of({ stopped_by: 'cancelled' })),
        running: stoppedByLabel(of({})),
        junk: stoppedByLabel(null),
      }));
    """)
    assert out["converged"] == "stopped early: the rounds converged (0.91)"
    assert out["noScore"] == "stopped early: the rounds converged (0.00)"
    assert out["rounds"] == "ran all 2 rounds" and out["one"] == "ran all 1 round"
    assert out["cancelled"] == "stopped: cancelled"
    assert out["running"] == "" and out["junk"] == ""


# ── the Merge button's prompt ──────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_merge_uses_the_prompt_the_server_assembled_and_rebuilds_it_otherwise():
    out = _run(f"""
      const R = {json.dumps(RUN)};
      const served = normalizeRun({{ ...R, result: {{ ...R.result,
        merge_prompt: 'THE SERVER ASSEMBLED THIS' }} }});
      console.log(JSON.stringify({{
        served: mergePromptFor(served),
        rebuilt: mergePromptFor(normalizeRun(R)),
        nothing: mergePromptFor(null),
        empty: mergePromptFor(normalizeRun({{ id: 'x', result: {{ final: [
          {{ entry: 0, model: 'a', text: '   ' }}] }} }})),
      }}));
    """)
    assert out["served"] == "THE SERVER ASSEMBLED THIS"
    rebuilt = out["rebuilt"]
    # the task, both finalists under neutral labels, and the fusion instruction
    assert "write a <b>CSV</b> parser" in rebuilt, "the composer gets the text, not escaped markup"
    assert "--- Solution A (ranked 1, judged 240/300) ---" in rebuilt
    assert "--- Solution B (ranked 2) ---" in rebuilt
    assert "A hybrid draft" in rebuilt and "B first draft" in rebuilt
    assert "complementary, not conflicting" in rebuilt
    # the model names are NOT in it — the composer prompt is anonymous too
    assert "qwen3.5:9b" not in rebuilt and "llama3:8b" not in rebuilt
    assert out["nothing"] == "" and out["empty"] == ""


# ── defensive shapes ───────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_defensive_payload_unwrapping_and_field_defaults():
    out = _run("""
      console.log(JSON.stringify({
        wrapped: normalizeRun({ run: { id: 'a', status: 'done' } }).id,
        bare: normalizeRun({ id: 'b' }).id,
        junk: normalizeRun(null),
        noResult: normalizeRun({ id: 'c' }).final,
        badNums: normalizeRun({ id: 'd', rounds: 'x',
          result: { answers: [{ entry: 'x', round: null, elapsed_s: 'y', tokens: null }],
                    final: [{ rank: 'x', total: 'y', tiebreak: null }] } }),
        scoresNull: normalizeFinal({ scores: { correctness: 'x' } }).scores,
        scoresMissing: normalizeFinal({}).scores,
        answersByEntry: [...answersByEntry(normalizeRun({ id: 'e', result: { answers: [
          { entry: 1, round: 1, text: 'b' }, { entry: 0, round: 1, text: 'c' },
          { entry: 1, round: 0, text: 'a' }] } })).entries()]
          .map(([k, v]) => [k, v.map(a => a.round)]),
        winner: (winnerOf(normalizeRun({ id: 'f', result: { final: [
          { model: 'x', rank: 2 }, { model: 'y', rank: 1 }] } })) || {}).model,
        winnerNoRanks: (winnerOf(normalizeRun({ id: 'g', result: { final: [
          { model: 'first' }] } })) || {}).model,
        winnerNone: winnerOf(normalizeRun({ id: 'h' })),
        live: [isLiveStatus('running'), isLiveStatus('done'), isLiveStatus(null)],
      }));
    """)
    assert out["wrapped"] == "a" and out["bare"] == "b"
    assert out["junk"]["id"] == "" and out["junk"]["final"] == []
    assert out["noResult"] == []
    assert out["badNums"]["rounds"] == 0
    assert out["badNums"]["answers"][0] == {"entry": 0, "model": "", "round": 0, "text": "",
                                            "elapsed_s": 0, "tokens": 0,
                                            "tokens_source": "estimated"}
    assert out["badNums"]["final"][0]["rank"] is None and out["badNums"]["final"][0]["total"] is None
    assert out["badNums"]["final"][0]["tiebreak"] == 0
    # a score the judge could not give stays null, it never becomes 0
    assert out["scoresNull"] == {"correctness": None, "completeness": None, "sophistication": None}
    assert out["scoresMissing"] == {"correctness": None, "completeness": None,
                                    "sophistication": None}
    assert out["answersByEntry"] == [[1, [0, 1]], [0, [1]]]
    assert out["winner"] == "y" and out["winnerNoRanks"] == "first"
    assert out["winnerNone"] is None
    assert out["live"] == [True, False, False]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_server_values_cannot_break_out_of_a_class_or_an_attribute():
    out = _run("""
      console.log(JSON.stringify({
        card: modelCardHtml(normalizeRun({ id: 'x', status: 'done',
          models: ['m"><img src=x>'], result: { answers: [] } }), 0, 'm"><img src=x>', [], {}),
        state: boardHtml(normalizeRun({ id: 'x', status: 'weird" onload="1',
          models: [], result: {} }), {}),
        token: trToken('run" onload="1'),
      }));
    """)
    assert out["token"] == "runonload1"
    assert "<img" not in out["card"] and 'onload="' not in out["card"]
    assert "&lt;img src=x&gt;" in out["card"]
    assert 'onload="' not in out["state"] and "trn-state-weirdonload1" in out["state"]


def test_the_board_follows_the_stream_and_falls_back_to_polling():
    """The page must consume the SSE endpoint it ships with, and must never be
    left frozen when the stream is unavailable.

    The bug this pins: the server's progress frames arrive UNNAMED, so they only
    reach `onmessage`. A page listening on a named event — or not listening at
    all, as this one originally did — shows a board that never moves while the
    run is live.
    """
    src = SRC
    assert "EventSource(" in src, "the page never opens the stream it ships with"
    assert "es.onmessage" in src, "unnamed progress frames only reach onmessage"
    assert "addEventListener('end'" in src, "the terminal frame is named 'end'"
    assert "_noStream" in src and "es.onerror" in src, \
        "a failed stream must latch and fall back to polling"
    # the fallback must still exist
    assert "setTimeout(poll, POLL_MS)" in src


def test_a_stream_and_a_poll_timer_never_run_at_once():
    """Both feeding the board would double every request for no extra freshness."""
    src = SRC
    assert "if (_stream) {" in src, "poll() must not reschedule while a stream feeds it"
