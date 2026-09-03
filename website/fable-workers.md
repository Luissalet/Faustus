---
layout: default
---

# Fable workers — let an expensive model plan and review while local workers do the work

The idea: Claude (Fable) in Cowork / Claude Desktop / Claude Code is good at
planning, judging and writing the final answer, and expensive per token. The
mechanical middle — read these files, change them, run the tests, fix what
fails, repeat — is where a tool loop burns tens of thousands of tokens. Faustus
already runs that loop on local models (`/agents` in a chat). **Dispatch** opens
that loop to an outside coordinator and answers with a compact result: per
worker the status, the files it changed, the tests/static checks, its last
words — a few hundred tokens, never a transcript.

```
Fable (plans)  ──POST /api/dispatch──▶  Faustus  ──▶  local workers (Ollama, both GPUs)
Fable (reviews) ◀── compact result ────  Faustus  ◀──  files changed, tests, summary
```

Everything a dispatched job does happens inside a **Workers chat** in Faustus:
the control board, steer/stop, the transcripts. The human can open it at any
time (`chat_url` in every answer).

## 1. Create a token

Settings → API tokens → new token, scope **Dispatch workers** (`agents:dispatch`)
— or the profile `fable_workers`. Nothing else: the token cannot chat, read
email or touch settings.

## 2. Pick the workers' model

Settings → Agent Tools → Agent & automation → Sub-agents:

* **Fable workers: model** — e.g. `qwen3.5:9b` (empty = the utility model,
  then the default chat model).
* **Fable workers: endpoint id** — only when the model lives on a server other
  than the default one.

With two cards, pin that model to one card in Settings → Local models →
Options… (`main_gpu`), so the workers do not fight your chat model for VRAM.

## 3a. Claude Desktop / Cowork / Claude Code: the MCP server

Add to the client's MCP configuration (`claude_desktop_config.json`, or the
MCP settings of Cowork / Claude Code):

```json
{
  "mcpServers": {
    "faustus-workers": {
      "command": "D:/LocalAI/odysseus/venv/Scripts/python.exe",
      "args": ["D:/LocalAI/odysseus/mcp_servers/workers_server.py"],
      "env": {
        "FAUSTUS_URL": "http://127.0.0.1:7000",
        "FAUSTUS_API_TOKEN": "ody_…",
        "FAUSTUS_MCP_FORMAT": "toon"
      }
    }
  }
}
```

Tools the coordinator gets: `workers_guide` (how to use the workers well —
read once per session), `dispatch_workers` (1–4 tasks, workspace, parallel,
reviewer, model), `workers_wait` (block until done, compact result),
`workers_status`, `workers_events` (the board's events, for a stuck worker),
`workers_cancel`, `workers_list`.

`FAUSTUS_MCP_FORMAT` (default `toon`, see [robot mode](#3c-robot-mode-one-envelope-and-toon))
decides what the row-shaped tools answer with. On `toon`, `objectives_list`,
`workers_status` and `guard_explain` ask the endpoint for `?format=toon` and
hand that envelope through — the machine view of the read, as rows: a third
to a half the characters of the same read in JSON, with no prose summarised
away. On `text` they go back to the one-glance human wording, which is also
the automatic fallback whenever the robot-mode call does not come back. The
two tools whose answer is not rows keep their wording either way: `memory_pack`
returns a prose block of learned rules, and `workers_events` a deliberate tail
(the last 80 of up to 400 events). Tool names and arguments never change.

**Making any Fable-type model understand it.** Three layers, so it works
whether the model reads skills, tool descriptions or nothing at all:

1. The tool descriptions themselves say what to send and what comes back.
2. `workers_guide` / `GET /api/dispatch/guide` returns the coordinator's
   guide (below) — when to dispatch, how to write a task, how to read a
   result, the plan → dispatch → wait → check loop.
3. `integrations/claude/skills/faustus-workers/SKILL.md` is a ready-made **skill** for
   Cowork / Claude Code: Claude Code gets it inside the bundle Settings →
   Integrations → Add a Claude Agent downloads (`/api/claude/plugin.zip` →
   `~/.claude/skills/`); for Cowork copy the folder into the client's skills
   (Settings → Skills → add). The model then picks the workers up on its own
   for file/test/refactor work.

In Cowork, then, this is the whole interaction:

> **You:** en `D:\proj` añade validación a `apply_discount` y su test, con los workers locales.
> **Fable:** *(dispatch_workers → workers_wait)* Hecho: el worker cambió `cart.py` y `tests/test_cart.py`, 7 tests en verde. Tablero: http://127.0.0.1:7000/#…

A good task is self-contained and says what "done" means: *"In `cart.py` add
`apply_tax(total, rate)` (rate as a fraction, rounds to cents) and a test in
`tests/test_cart.py`; `pytest -q` must pass."* The coordinator keeps the plan,
the ordering and the review; it does not read files through the workers.

## 3b. Any HTTP client

```powershell
$h = @{ Authorization = "Bearer ody_…" }
$job = Invoke-RestMethod -Method Post http://127.0.0.1:7000/api/dispatch -Headers $h -ContentType application/json -Body (@{
  tasks = @("Add apply_tax(total, rate) to cart.py with a test; pytest must pass")
  workspace = "D:\proj"; parallel = $true; verify = "python -m pytest -q"
} | ConvertTo-Json)
Invoke-RestMethod "http://127.0.0.1:7000/api/dispatch/$($job.id)/wait?timeout=600" -Headers $h | ConvertTo-Json -Depth 6
```

| Route | What |
|---|---|
| `POST /api/dispatch` | `{tasks, workspace, model?, parallel?, reviewer?, max_rounds?, timeout_s?, context?, verify?, verify_scope?, fix_rounds?, client_request_id?}` → the job. Header `Idempotency-Key`: a retry returns the same job |
| `GET /api/dispatch/{id}` | status; per-worker progress, `phase`, `ceiling_s` and `wait_again` while it runs; the compact result when done |
| `GET /api/dispatch/{id}/wait?timeout=N` | long-poll up to 1800 s |
| `GET /api/dispatch/{id}/events` | the board's last 400 events |
| `POST /api/dispatch/{id}/cancel` | stop it (`cancelling` until the workers have unwound, then `cancelled` with what changed) |
| `GET /api/dispatch` | recent jobs |
| `GET /api/dispatch/config?workspace=` | the model/server a job would run on and the verifier Faustus would run in that folder |

Admins only (a plain user gets a 403 before the request is looked at: a
worker runs shell commands in any folder the caller names). Jobs are
mirrored to `DATA_DIR/dispatch/<id>.json` (the newest 200 are kept); a job
the server was restarted under reads back as `interrupted`.

## 3c. Robot mode: one envelope, and TOON

The reads a machine cares about answer in a **standard envelope** when you
ask for it, so a coordinator writes one parser instead of one per endpoint:

```json
{"ok": true, "data": {…}, "error_code": null, "error": null,
 "elapsed_ms": 12, "schema_version": 1}
```

`ok` is exactly `error_code is null` — on a failure too, so a missing job is
`{"ok": false, "error_code": "http_404", "error": "no such dispatch job", …}`
with the 404 still on the response, never FastAPI's bare `{"detail": …}`.

Two query parameters turn it on, per request:

| Parameter | Answer |
|---|---|
| *(none)* | exactly what the browser page gets today, byte for byte |
| `?robot=1` | the lean projection, in the envelope, as JSON |
| `?format=toon` | the same envelope as **TOON**, `text/plain; charset=utf-8` |

On: `GET /api/dispatch/{id}`, `GET /api/dispatch/{id}/events`,
`GET /api/projects/{id}/objectives`, `GET /api/memory-engine/items`,
`GET /api/memory-engine/pack`, `GET /api/command-guard/log`,
`GET /api/command-guard/explain`, `GET /api/system/usage`.

**What robot mode sends is not the page's payload.** It is the *machine view*
of the read (`src/robot_projection.py`): the same facts as flat, scalar-only
rows — one row per memory item, objective, receipt, GPU, loaded model, worker,
event. A list inside a row becomes one cell (an objective's `deps` becomes
`blocked_by: OBJ-3,OBJ-2`), a 32-character id becomes its `id8`, a worker's
file list becomes a count beside the job-level union of the paths, and what
the coordinator already knows — the task instructions it sent, the enum tables
the UI paints dropdowns from, the per-receipt chain digests only the server
walks — is left out. The plain, no-parameter answer still carries all of it.

**TOON** (Token-Oriented Object Notation, `src/toon.py`) is a line-oriented
encoding for exactly this: a uniform array of objects is written as one
header naming the keys plus one comma-joined line per row, instead of naming
every key again in every record. `GET /api/command-guard/log?format=toon`:

```
ok: true
data:
  receipts[3]{ts,tool,tier,rule,action,command_head,note,hash8}:
    2026-08-30T12:34:56+00:00,bash,DANGEROUS,fs.rm_rf,blocked,rm -rf build/,"",9f1c8e0a
    2026-08-30T12:35:02+00:00,bash,SAFE,"",allowed,pytest -q,"",3b77a201
    2026-08-30T12:36:11+00:00,python,CAUTION,fs.write,allowlisted,"open('out.txt','w')",the allowlist entry expires in 40 minutes,c04e91bd
  chain:
    ok: true
    length: 3
    broken_at: null
error_code: null
error: null
elapsed_ms: 4
schema_version: 1
```

### What it really saves

Measured end to end — the bytes of `?format=toon` against the bytes of the
plain JSON body of the same read, on realistic payloads (memory items carrying
three helpful and two harmful feedback events apiece, objectives with deps and
hints, a two-GPU box with three models loaded, a job with three workers and a
failed verification). The fixtures and the assertions are in
`tests/test_robot_projection.py`:

| Read | plain JSON | envelope only | **lean** | Saved |
|---|---|---|---|---|
| `/api/memory-engine/items?limit=5` | 8085 chars | 8513 (1.05×) | **1364** | **83 %** |
| `/api/command-guard/log?limit=25` | 11484 chars | 12553 (1.09×) | **3883** | **66 %** |
| `/api/dispatch/{id}` | 4962 chars | 5993 (1.21×) | **1974** | **60 %** |
| `/api/projects/{id}/objectives` | 5676 chars | 6726 (1.18×) | **2310** | **59 %** |
| `/api/system/usage` | 2567 chars | 3274 (1.28×) | **1321** | **49 %** |
| `/api/dispatch/{id}/events` | 3710 chars | 4749 (1.28×) | **1992** | **46 %** |

The middle column is why the projection exists, and it is worth reading before
reaching for TOON anywhere else. Enveloping a real payload and re-encoding it
**as it stands** is a LOSS on every one of these reads — against a running
instance it measured 1.15× on the memory items, 1.24× on the objectives, 1.23×
on the usage document, and a 7 % win on the guard log. TOON is paid for by keys
repeated once per row, and none of these payloads had rows: every memory item
carries its evidence and feedback arrays, every objective a `deps` list and a
score in a separate per-id object, every GPU its own model list, every receipt
an optional `note` most records lack — so nothing collapsed into a table and
the two-space indent per nesting level cost more than JSON's braces. (A cell
holding a comma or a quote is quoted, as the third receipt above shows, so a
row always parses back into exactly the values it was written from.)

Projecting first is what makes the table fire, and then the encoding pays.
Both robot modes send the lean view: `robot=1` when you want it as JSON,
`format=toon` when you want it small.

Round-tripping is the property the tests hammer: `toon.decode(text)` gives
back exactly the object Faustus meant to send — nesting, folded single-key
paths (`config.database.host: localhost`), quoted cells holding commas or
quotes, unicode, empty containers, and strings that merely look like numbers
(`"3"` stays a string). A decoder is ~80 lines; the format is documented in
full at the top of `src/toon.py`. Two reads have no projection because they
are already scalars and prose: `GET /api/memory-engine/pack` (the block a
worker would be given, verbatim) and `GET /api/command-guard/explain` (one
classification of one command).

## What makes the answer trustworthy

None of it is taken from the worker's word:

* **Evidence.** The workspace is checkpointed before the job (the harness's
  shadow repo — the user's own `.git` is never touched) and diffed after
  it. `result.changes` (`added` / `modified` / `deleted`, content-exact) is
  what really changed; `result.files_changed` is that list; a file a worker
  *said* it changed but did not shows up as `result.claimed_only`. Without
  git on the box an mtime snapshot of the tree does the same job.
* **Verification by Faustus.** After the workers, Faustus runs `verify` in
  the workspace itself — the command you gave (`pytest -q`, `npm test`,
  `make check`…) or, with `auto`, the project's detected test runner over
  the tests related to the changed files (`verify_scope: "all"` for the
  whole suite). Failures are compared with the checkpoint: a test that
  already failed before the job is `pre_existing` and does not block.
  `result.verification` carries `ok`, `summary`, `failures`, `command`,
  `output_tail`. No runner and no command → `ok: null`, "not verified" —
  never "passed".
* **One bounded fix round.** When the verification fails, one fixer worker
  gets the failing command's output plus the original tasks, and the
  verification runs again (`fix_rounds`, default 1, max 2; `attempts` in
  the result says how many). Still failing → the job is `partial`.
* **Honest status.** `done` only when every worker finished and the
  verification passed or could not run; `partial` when a worker ended
  `error` / `timeout` / `stalled` / `stopped` or the verification failed;
  `verdict` says it in one line (`1/2 workers done (timeout) · 3 files
  changed on disk · verification FAILED (1 failed)`).
* **One machine.** The "at most N workers at once" semaphore is shared by
  every delegation on the endpoint (a chat's `/agents` and two jobs at the
  same time no longer run 3 × N workers against one Ollama); jobs in the
  same workspace (or a nested one) run one at a time — the second waits as
  `queued` and says so in `phase`; a worker's file locks are released when
  it finishes, so a dependent task in a sequential run may edit what the
  previous one wrote.
* **Bounded answer.** Per worker: the failures of the static checks, at most
  40 claimed paths, 1200 chars of last words; the tree's git state once per
  job. A 4-worker worst case is ~2.5k tokens; a real job ~500.

The coordinator's text is written into the Workers chat marked as external,
untrusted context (the tool gate treats it like any pasted document), and
`gen_overrides` may carry sampling knobs only — never `main_gpu`, `num_gpu`
or `keep_alive`, which would override the GPU placement policy.

## The coordinator's guide (what `workers_guide` returns)

    # Using Faustus workers (for the coordinating model)

    You are the planner and the reviewer. The workers are local models on the
    user's machine: cheap, tireless, good at mechanical steps, weaker at judgement.
    Your own tokens are the scarce resource — spend them on deciding WHAT to do
    and on checking the result, not on reading files or running tests yourself.

    ## When to dispatch
    - Editing or creating files, running tests/linters/builds, fixing what fails,
      refactors with a clear spec, searching a codebase, converting formats,
      writing boilerplate or docs from a spec: dispatch.
    - Deciding the design, judging trade-offs, anything ambiguous, anything the
      user must decide, the final answer to the user: keep.

    ## How to write a task (each task = one worker)
    1. Self-contained: name the files, the function/class, the behaviour, and the
       exact command that proves it ("`pytest -q` in the workspace must pass").
       The worker does not see this conversation — everything it needs goes in
       the instruction or in `context`.
    2. One outcome per task. Two changes that touch the same file go in ONE task
       (parallel workers lock files against each other).
    3. Say what NOT to do when it matters ("do not touch the public API",
       "keep Python 3.11 compatibility").
    4. 1–4 tasks per job. Independent tasks → `parallel: true`; dependent ones →
       `parallel: false` (they run in order, and a later task may edit what an
       earlier one wrote). Jobs in the same workspace run one at a time.
    5. `workspace` is required: the folder the workers are confined to.
    6. `verify` is the command that proves the job is done (`pytest -q`,
       `npm test`, `make check`…). Faustus runs it ITSELF after the workers, in
       the workspace — the workers' own claims are never the proof. Without it
       the project's test runner is auto-detected (`verify: "auto"`);
       `verify_scope: "all"` runs the whole suite instead of the tests related
       to the changed files. `fix_rounds` (default 1, max 2): when the
       verification fails, one fixer worker gets the failure output and the
       verification runs again.

    ## Reading the result
    `status`: `done` = every worker finished AND the verification passed (or
    could not run — read `verification.summary`); `partial` = some worker ended
    `error` / `timeout` / `stalled` / `stopped`, or the verification failed;
    `error` = nothing ran; `cancelled` / `interrupted` = stopped early (the
    evidence is still there). `verdict` says it in one line.
    `result.changes` is what Faustus SAW change on disk (checkpoint diff) —
    `result.files_changed` is that list; `result.claimed_only` names files a
    worker said it changed but did not. `result.verification` is the run Faustus
    made: `ok`, `summary`, `failures`, `pre_existing` (failed before the job
    too), `command`, `output_tail`; `attempts` > 1 means a fix round ran.
    Per worker: status, files it claims, tool/round/token counts and its last
    words (≤ 1200 chars) — never the transcript. Trust `changes` + `verification`
    over the prose.
    A `running` answer carries `progress` per worker, `phase`, `ceiling_s` (the
    most it can still take) and `wait_again: true` — call `workers_wait` again;
    do NOT re-dispatch the same task because one wait returned early.

    ## Loop
    plan → dispatch → wait (again if still running) → read verdict + changes +
    verification → (dispatch a narrower fix if needed) → answer the user.
    Do not re-do a worker's work yourself; send a narrower task instead. Tell the
    user which changes came from the workers and point them at the board
    (`chat_url`) if they want the details.

## What the workers can and cannot do

The same rules as `/agents`: confined to the `workspace` folder, file locks
between parallel workers, the watchdog and supervisor (a stalled worker is
nudged once, then stopped), the lean toolset, the GPU semaphore. The model
inside a Faustus chat cannot call `/api/dispatch` (it has `delegate_agents`,
behind the chat's own gate); a token without `agents:dispatch` gets a 403.
