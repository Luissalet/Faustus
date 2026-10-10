# Cookbook job logs

Every session the Cookbook launches (a model download, a serve, a dependency
install) leaves a durable record of what ran and what it printed. All routes are
admin only and read only. A job id is the session id (`serve-1a2b3c4d`,
`cookbook-5e6f7a8b`).

## What is recorded

| Field | Meaning |
|---|---|
| `command` | The command as executed, e.g. `hf download org/model`. Secret flags (`--token X`) are replaced by `***`. |
| `argv` | The launcher's argv (for a local Windows job, Git Bash and the wrapper script). |
| `cwd` | Working directory of the launcher. |
| `env` | Environment keys handed to the process. Values of secret-looking names (`*TOKEN*`, `*KEY*`, `*PASSWORD*`, `*SECRET*`...) are `***`. |
| `started_at`, `ended_at`, `duration_s` | ISO times; `ended_at` is set the first time the status poller sees a terminal status. |
| `status`, `exit_code` | Last status seen and the runner's exit code once printed. |

Output lives in up to three files: `combined`, `stdout`, `stderr`. A local
detached launch writes all three. A tmux or SSH session only exposes a pane
capture, so only `combined` exists (a capped copy of the latest capture) and the
other two report `stream_available: false`. Each file is capped at 20 MB (the
head is trimmed when the job ends); a read returns at most the last 2000 lines
and redacts secrets again.

## Routes

`GET /api/cookbook/jobs?limit=50` returns `{"jobs": [...]}`, newest first, no output.

`GET /api/cookbook/jobs/{id}/log?stream=combined&tail=200`

```json
{
  "job": {"session_id": "serve-1a2b3c4d", "exit_code": 1, "command": "llama-server ...", "...": "..."},
  "stream": "combined",
  "tail": 200,
  "available_streams": ["combined", "stdout", "stderr"],
  "stream_available": true,
  "lines": [{"text": "loading", "stderr": false}, {"text": "CUDA error: out of memory", "stderr": true}],
  "total_bytes": 1234,
  "truncated": false,
  "header": "# Faustus Cookbook job serve-1a2b3c4d\n..."
}
```

`tail` is clamped to 1..2000. `format=text` returns the header plus the lines as
plain text (stderr lines prefixed `[stderr]` in the combined stream). In
`combined`, a line is flagged `stderr: true` when the same line appears in the
stderr file.

`GET /api/cookbook/jobs/{id}/download?stream=combined` streams the whole file as
`text/plain` with a `Content-Disposition: attachment` header and the job header
in front.

Errors are flat `{"error", "error_class"}` bodies: `400 cookbook.bad_request`
(bad id or stream), `404 cookbook.job_not_found`, `404 cookbook.stream_unavailable`.

## MCP

The built-in `harness` server exposes the read-only tool `cookbook_job_log`:
`job_id` (omit to list recent jobs), `stream`, `tail`, `limit`. It returns the
same body as the log route.

## Limits

- The order between stdout and stderr lines in `combined` is not guaranteed for
  local jobs (two `tee` processes feed it).
- A runner script that already merges stderr into stdout (the `hf` installer
  step does) leaves the `stderr` stream empty.
- Sessions started before this feature have no record.
