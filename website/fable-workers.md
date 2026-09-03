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
        "FAUSTUS_API_TOKEN": "ody_…"
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

**Making any Fable-type model understand it.** Three layers, so it works
whether the model reads skills, tool descriptions or nothing at all:

1. The tool descriptions themselves say what to send and what comes back.
2. `workers_guide` / `GET /api/dispatch/guide` returns the coordinator's
   guide (below) — when to dispatch, how to write a task, how to read a
   result, the plan → dispatch → wait → check loop.
3. `integrations/faustus-workers/SKILL.md` is a ready-made **skill** for
   Cowork / Claude Code: copy the folder into the client's skills directory
   (Cowork: Settings → Skills → add; Claude Code: `.claude/skills/`) and the
   model picks the workers up on its own for file/test/refactor work.

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
  workspace = "D:\proj"; parallel = $true
} | ConvertTo-Json)
Invoke-RestMethod "http://127.0.0.1:7000/api/dispatch/$($job.id)/wait?timeout=300" -Headers $h | ConvertTo-Json -Depth 6
```

| Route | What |
|---|---|
| `POST /api/dispatch` | `{tasks, workspace?, model?, parallel?, reviewer?, max_rounds?, timeout_s?, context?}` → the job |
| `GET /api/dispatch/{id}` | status; per-worker progress while it runs; the compact result when done |
| `GET /api/dispatch/{id}/wait?timeout=N` | long-poll up to 600 s |
| `GET /api/dispatch/{id}/events` | the board's last 400 events |
| `POST /api/dispatch/{id}/cancel` | stop it |
| `GET /api/dispatch` | recent jobs |

Jobs are mirrored to `DATA_DIR/dispatch/<id>.json`; a job the server was
restarted under reads back as `interrupted`.

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
       `parallel: false` (they run in order) or separate jobs.
    5. `workspace` is the folder the workers are confined to. Always set it.

    ## Reading the result
    `workers_wait` returns, per worker: status (`done`, `error`, `timeout`,
    `stalled`, `stopped`), files changed, static checks, git state, tool/round
    counts, and the worker's last words (≤ 1200 chars). It never returns the
    transcript. Trust files changed + tests over the worker's prose: if the
    summary claims a change but `files_changed` is empty, it did not happen.
    A worker that ended `stalled` or `timeout` did part of the work — look at
    `files_changed`, then dispatch the remainder as a new, narrower task.

    ## Loop
    plan → dispatch → wait → check → (dispatch fixes) → answer the user.
    Do not re-do a worker's work yourself; send a narrower task instead. Tell the
    user which changes came from the workers and point them at the board
    (`chat_url`) if they want the details.

## What the workers can and cannot do

The same rules as `/agents`: confined to the `workspace` folder, file locks
between parallel workers, the watchdog and supervisor (a stalled worker is
nudged once, then stopped), the lean toolset, the GPU semaphore. The model
inside a Faustus chat cannot call `/api/dispatch` (it has `delegate_agents`,
behind the chat's own gate); a token without `agents:dispatch` gets a 403.
