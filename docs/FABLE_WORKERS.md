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

Tools the coordinator gets: `dispatch_workers` (1–4 tasks, workspace, parallel,
reviewer, model), `workers_wait` (block until done, compact result),
`workers_status`, `workers_events` (the board's events, for a stuck worker),
`workers_cancel`, `workers_list`.

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

## What the workers can and cannot do

The same rules as `/agents`: confined to the `workspace` folder, file locks
between parallel workers, the watchdog and supervisor (a stalled worker is
nudged once, then stopped), the lean toolset, the GPU semaphore. The model
inside a Faustus chat cannot call `/api/dispatch` (it has `delegate_agents`,
behind the chat's own gate); a token without `agents:dispatch` gets a 403.
