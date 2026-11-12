# Configuration improvement cycles

Studio → Skills → Improvement cycles starts or schedules an experiment over
the current configuration. This extends `harness_evolution` (CandidatePatch,
versioned revisions, CAS) and consumes `harness_refinement` signals and the
daily battery's deterministic checks. The existing proposal/probe and creator
preset flows remain available.

## Experiment contract

Give a target group and a fixed battery containing that group and at least
one other group. Every case has an id, group, prompt and explicit checks.
The configured global model proposes one small typed edit per candidate;
an explicit endpoint/model can select a different configured service for an
experiment. There is no local model fallback.

The baseline and each candidate run the same cases and number of repeats.
For every group the score is the arithmetic mean of case scores in [0,1].
The target must improve strictly; each other group's loss must be at most
`regression_threshold` (default .01 = one percentage point). Duplicate or
missing ids, changed groups, non-finite scores, provider/judge errors and
unavailable judgments make the comparison insufficient. A group's loss is
never hidden by a gain in another group. This is a small-battery comparison,
not a significance test or a claim of general model improvement.

Candidates are evaluated sequentially. After an accepted edit, the next
candidate's control is the configuration now in use. A concurrent configuration
change makes the candidate stale; it is never automatically rebased and applied
using old evidence. Rejected trials never change the active revision.

## Typed sidecars

Each runtime edit is stored in the existing revision's `runtime_config`, scoped
to its owner and optional project. Source files and the original system prompt
remain intact. Normal agent turns freeze the effective sidecars and apply them
when composing messages, options and tool schemas. Project sidecars override
the owner's matching unscoped key. Explicit turn context/temperature choices
take precedence.

| Kind | Key and value | Handling |
| --- | --- | --- |
| `system_section` | response, reasoning, workflow, verification; 1–2000 characters, ≤30 lines | Appends a named editable section |
| `model_parameter` | temperature 0–2; max_tokens 64–32768 | Generation options |
| `tool_description` | tool name, text ≤2000 characters | Replaces that offered schema's description |
| `tool_selection` | tool name, boolean | Selects or omits a tool from the model's offered set; existing execution policy still applies |
| `skill` | existing skill name, boolean | Enables its body or omits it from automatic skill selection |
| `context` | no_memory/no_skills boolean; input_token_budget 4096–131072 | Existing context options |

Workflow-section, tool-selection, skill and context edits are classified high
risk and wait for the person in Studio. Other edits apply automatically after
passing the comparison. Each patch can be reverted independently; unrelated
later edits are retained. A later edit to the same setting must be undone first.
The generic harness rollback route delegates to this same individual undo.

## Cases and evidence sources

`completion` mode compares a prompt-only configuration using `base_system` and
the real model service. It cannot evaluate tool/context edits. `agent` mode
uses the existing read-only refinement rollout and normal agent loop, including
the same sidecar composition as normal turns. The daily battery imports as
agent cases; write cases are excluded. A read-only rollout does not certify
side effects, and a completion experiment does not certify an agent's full
tool-using behavior. The before/after answers, errors, checks and group scores
are retained per candidate.

Selected `session_ids` (or the owner's 20 recent sessions) contribute existing
corrections, repetitions, tool failures and model-call errors. Existing daily
evaluation reports contribute failed checks. `galton_run_ids` imports failures
from selected prior Galton evaluations. These observations inform proposals;
explicit case checks decide the result.

Galton now lives inside the evaluation module of Pygmalion. Import suites with
`POST /api/harness/cycles/galton/cases` (`url`, `suites`) or enter cases with
`galton_checker`. Set `galton_url` to the already enabled native evaluation
base (including `/evaluation` when using the unified app). This calls its
existing `suite_get`, `case_get`, `run_results` and `answer_check`, without
starting or enabling the module. Tool-bearing Galton cases need explicit agent
cases. If authentication is needed, `FAUSTUS_EVALUATION_TOKEN` supplies the
existing evaluator token; it is never stored in the cycle specification.

## HTTP and MCP

- `GET /api/harness/cycles/cases`: daily battery.
- `POST /api/harness/cycles/galton/cases`: import native Galton suites.
- `POST /api/harness/cycles`: CycleSpec below; returns queued cycle immediately.
- `GET /api/harness/cycles`: owner's history.
- `GET /api/harness/cycles/{id}`: cycle and full CandidatePatch evidence.
- `POST /api/harness/cycles/patches/{id}/revert`: individual rollback.
- `POST /api/harness/cycles/patches/{id}/accept`: person applies a high-risk
  evaluated candidate; the revision must still match its tested control.
- Built-in harness MCP: `improvement_cycle_start` (`spec`),
  `improvement_cycle_status` (optional `id`), `improvement_cycle_revert`
  (`patch_id`). Set `ODYSSEUS_MCP_HARNESS_OWNER` for that MCP process.

```json
{
  "name": "Answer format",
  "target_group": "format",
  "cases": [
    {"id": "sum", "group": "format", "prompt": "Return only 6 + 7 as a number.", "checks": {"answer_all": ["^13$"]}},
    {"id": "capital", "group": "geography", "prompt": "Name Spain's capital.", "checks": {"answer_all": ["Madrid"]}}
  ],
  "regression_threshold": 0.01,
  "candidates": 2,
  "repeats": 2
}
```

To schedule, the panel creates an ordinary `/api/tasks` action with
`action: improvement_cycle` and `prompt: JSON.stringify(spec)`. Recurrence,
time zone, leases, notifications and cancellation use the existing scheduler;
there is no second background timer. The action is also available to API clients.
