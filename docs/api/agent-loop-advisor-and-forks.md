# Agent loop: advisor, typed decisions, declared risk, stuck watch, path rules

Everything here is opt-in or bounded, advisory where a model is involved, and
total: any failure keeps the loop's previous behaviour. Settings live in
Settings -> Agent (group "Sub-agents"; the per-mode effort under "Reasoning
per mode") and in `src/settings.py`; the Agent screen shows a Spanish label
and help for each.

## Advisor (`src/advisor.py`)

A second model that reads the whole session and advises. It never acts, never
calls a tool and never speaks to the user. The teacher takes over after a
failure; the advisor is consulted before one.

Three triggers, decided in code from turn state, never by the model:

| trigger | when |
| --- | --- |
| `plan_or_write` | the first round of a turn that creates a plan (`update_plan`) or calls a tool that writes (once per turn) |
| `loop` | the loop breaker reaches its nudge step, and a monologue is detected (see below): the advisor is asked what to try instead of a generic note |
| `final` | before the final text of a turn that wrote files (once per turn) |

The advisor is handed the transcript so far (system prompt summarised, tool
names, every call and result, cut to a token budget, fenced as untrusted data)
and answers in at most ~300 words: "do X, not Y, because Z". The text is
injected as a runtime note marked advisory and untrusted. It is never treated
as approval.

| setting | default | meaning |
| --- | --- | --- |
| `advisor_enabled` | off | master switch |
| `advisor_model` | empty | model to ask; empty = the teacher's model resolution |
| `advisor_max_uses` | 3 | most uses per turn, across the triggers |
| `advisor_max_tokens` | 1024 | token limit of one answer |
| `advisor_context_tokens` | 16000 | how much of the session it reads |
| `mode_effort_advisor` | auto | reasoning effort of the advisor call (auto = medium) |

With no model configured the advisor is unavailable and every trigger falls
back to what the loop always did. An unreachable advisor costs one bounded
call and turns itself off for the rest of the turn. `teacher`, `ask_teacher`,
`doubt_review` and `auto_review` are unchanged.

Observability: `advisor_advice` events in the stream (collapsible item in the
turn activity), `metrics["advisor"]`, and `GET /api/agent/advisor/stats`
(counters and the last uses without their text).

### A/B in the daily battery

`scripts/daily_eval.py --advisor-ab` runs every case twice, back to back,
with `advisor_enabled` off and then on. It restores the setting afterwards
(also on failure) and reports, per arm and per case: hits (all checks
passed), model rounds, seconds and advisor uses, in the `.json` and `.md`
report. It needs an advisor or teacher model to be configured, otherwise both
arms behave the same.

## Typed decisions with receipts (`src/decision_forks.py`)

A closed question is answered from the next-token probabilities of the
allowed answers (`src/typed_decision.py`): one prefill, a confidence and a
probability mass, and "unknown" when either is low. Three forks, each behind
its own setting, all off by default:

| fork | setting | question |
| --- | --- | --- |
| `tool_error` | `typed_decision_error_fork` | after a failed tool call: retry the same / change the arguments / other tool / stop. Above the confidence threshold the choice is injected as an advisory hint and the model still acts |
| `tool_tie` | `typed_decision_tool_tie`, `typed_decision_tool_tie_tolerance` | when the top three lexical scores of the tool catalogue tie, which tied tool is offered first |
| `compaction_keep` | `typed_decision_compaction_keep` | in `extract` compaction, per old tool result: keep verbatim or fold into the digest; the last six messages are never folded |

Below the threshold the working model decides in its normal round. Each
decision writes a receipt: options, choice, confidence, mass, whether the old
behaviour was kept (fallback) and the outcome. Receipts go to
`metrics["decision_receipts"]` of the turn and to the counters behind
`GET /api/agent/decision-forks/stats`. They are the raw material for
measuring calibration later: recorded confidence against what actually
happened.

## Declared risk (`src/self_declared_risk.py`)

State-changing tools are offered with one extra optional parameter,
`security_risk` (`LOW | MEDIUM | HIGH | UNKNOWN`), added centrally where the
loop assembles the schemas. The declaration is a one-way lever: `HIGH` forces
the approval card even when policy would have run the call (unless the person
already chose "full" approval mode or allowed the task); the other values
never lower anything. The parameter is stripped before the tool runs. When
the declared level and the gate's own assessment disagree it is logged.
Setting: `self_declared_risk` (on). Counters: `GET /api/agent/risk/stats`,
`metrics["risk_declarations"]`.

## Stuck watch (`src/loop_breaker.py`, `StuckWatch`)

`LoopPolicy` sees tool calls repeat. These three detectors see what does not
look like a repeated call. All are deterministic counters, never read from
what the model says about itself; one per turn; a limit of 0 switches a
detector off. Events are kept in `metrics["stuck_watch"]`.

| detector | setting (default) | intervention |
| --- | --- | --- |
| Monologue: assistant rounds in a row that said something and called no tool, in a turn that has tools and that the loop held open (a plain answer ends at its first such round) | `agent_loop_breaker_monologue_rounds` (3) | first time: the advisor is asked (trigger `loop`) or a plain note tells the model to act with a tool or answer; the count restarts. Second time: the turn stops with what was said (`loop_breaker_stop`, trigger `monologue`). A round cut at the token limit and continued, and rounds rejected by the harness's own bounded correction, do not count |
| Context window: the provider refuses the request as too long (recognised from the error's wording, not the bare HTTP status) | `agent_loop_breaker_context_error_limit` (2) | first time: forced compaction (every stage, fewer recent tool rounds kept verbatim), the round is redone without spending the step budget. Consecutive errors up to the limit: the turn stops with a plain message ("start a new chat, shorten the input or use a model with a larger window") and the provider text attached; it is not retried again. A round the provider accepts resets the count |
| Failing path: the same tool with the same arguments returns an error | `agent_loop_breaker_failed_path_limit` (3) | the call is named closed in a runtime note; the next identical attempt is refused without running. A success of the same call reopens it. Other arguments and the tool itself are untouched |

## Path-scoped project rules (`src/project_rules.py`)

Project rules (`.faustus/rules`, `.agents/rules`, `.claude/rules`,
`.cursor/rules`) apply at the start of every turn. A rule can instead declare
globs in its frontmatter:

```
---
paths: src/api/**/*.py, tests/api/*.py
---
- validate every input
```

(`.mdc` files use `globs:`; `alwaysApply: true` keeps them on every turn.) A
scoped rule is not in the turn-start prompt, which only names it. Its text is
appended to the tool result the first time, in a conversation, the agent
touches a matching path:

- tools: `read_file`, `write_file`, `edit_file`, `apply_patch` (their files)
  and `ls`, `glob`, `grep` (their folder: a rule armed by a folder names files
  inside it; a bare `*.py` pattern names no folder, so it does not arm a
  listing);
- once per conversation and per rule text: the delivered set is kept in
  memory and in `DATA_DIR/project_rules_delivered.json` (bounded), so a
  restart does not repeat it; an edited rule is delivered again;
- budget: `project_rules_budget_tokens` bounds what one result gets. A rule
  that does not fit is named ("not shown, over budget") and delivered with the
  next matching touch; a single rule above the budget is cut with a pointer;
- Windows and POSIX spellings of a path (`C:\repo\src\a.py`,
  `c:/repo/src/a.py`, `src\a.py`, absolute or relative to the rule's root)
  match the same way; a path outside the rule's root never matches;
- glob syntax: `*` and `?` stay inside a segment, `**` crosses segments, a
  pattern without `/` matches at any depth, a trailing `/` means the whole
  folder;
- the folder must be approved (same gate as every project instruction);
  the patterns are part of the approved digest, so changing which files a rule
  covers asks for approval again. Existing digests are unchanged.

UI: Project -> Rules. Each installed `.md` rule shows "only for ..." or
"every turn" and a field for its path patterns (comma separated).
`POST /api/rules/paths {workspace, id, paths, origin?}` rewrites only the
`paths:` line of a rule file inside the workspace (other frontmatter and the
body are kept; an empty list makes it a turn-start rule again);
`GET /api/rules` returns `paths` per rule. Limits: 20 patterns of 200
characters, no commas.
