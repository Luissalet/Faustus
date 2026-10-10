# Degradation diagnostics

One report of what Faustus is running without: vector store, web search, notifications, email, model
endpoints, Sparks and MCP servers. Informational only; nothing is blocked.

Code: `src/degradation.py` (registry, checks, cache, `since` tracking). It reuses `src/service_health.py`
(ChromaDB, SearXNG, ntfy, IMAP, providers), `src/sparks.py` and `McpManager.get_all_statuses()`.

## API

`GET /api/diagnostics/degradation` (admin). `?refresh=1` re-runs the checks now.

```json
{
  "generated_at": "2026-10-10T10:00:00+00:00",
  "overall": "down",
  "counts": {"ok": 3, "degraded": 0, "down": 1, "unconfigured": 3},
  "items": [{
    "id": "searxng",
    "label": "Web search",
    "state": "down",
    "cause": "ConnectionRefusedError: connection refused",
    "since": "2026-10-10T09:12:44+00:00",
    "last_checked": "2026-10-10T10:00:00+00:00",
    "latency_ms": 14,
    "fix_hint": {"es": "...", "en": "...", "command": "docker compose up -d searxng"},
    "affects": ["web search", "deep research"]
  }],
  "problems": ["the degraded and down items only"],
  "cached": true,
  "age_s": 4.2
}
```

- `state`: `ok`, `degraded` (partial, or the probe did not answer in time), `down` (configured and failing) or
  `unconfigured` (off or not set up; never counts against `overall`).
- `cause` is the real error class plus a short message, scrubbed of URL credentials, tokens and keys. Empty when ok.
- `since` is the first time the check was seen in its current state. It is stored in
  `DATA_DIR/degradation.json` and survives restarts.
- `fix_hint` is empty for `ok` and `unconfigured` items.

## Behaviour

- Each check runs in its own worker thread with a deadline (3 s; 4.5 s for web search and ntfy, whose probe waits up to 4 s; 6 s for email and
  providers, which probe several accounts or endpoints). Overrun: `degraded` with `TimeoutError`. Exception: `down` with the exception class.
- The report is cached 20 s. A stale cache is still served and refreshed in the background (single flight); only the
  very first call waits (at most 9 s).
- Never blocks or gates a request or feature.

## MCP

Tool `degradation_status` of the built-in `harness` server (`mcp_servers/harness_server.py`), read-only.
Arguments: `refresh` (probe now), `only_problems`. It returns the running app's last report when it is under 2
minutes old (`source: app_snapshot`); otherwise it probes from its own process (`source: standalone_probe`), which
cannot see the vector store or the app's MCP servers.

## Studio

The Vitals panel > "Running degraded" lists only degraded and down items with cause, since when, what is
affected and the fix hint in the interface language. Hidden for non-admins and when everything is fine.

## Adding a check

```python
from src import degradation as dg
dg.register(dg.Check("tts", "Voice", lambda ctx: dg._result(dg.OK),
                     affects=["read aloud"], hint={"es": "...", "en": "...", "command": ""}))
```

A check receives `ctx` (`inputs` with settings, integrations, accounts and endpoints, plus `rag_manager`,
`memory_vector` and `mcp_manager` when running inside the app) and returns `dg._result(state, cause)`.

## Limits

No TTS/STT or external Hoard checks yet. The MCP process alone does not see app-bound dependencies.
