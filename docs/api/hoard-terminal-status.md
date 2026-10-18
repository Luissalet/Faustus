# Copilot turn status

`GET /api/hoard-copilot/turns/{session_id}/status` reads an existing turn.
The existing MCP `hoard_turn_status` operation uses this endpoint. Its inputs are
`session_id` and optional `run_id`. No new MCP operation or execution ledger is
introduced. Reading status never starts, resumes or replays a turn.

The selected session must belong to the caller, using the same owner check as
the native chat status endpoint. An explicit run ID selects only that run of
that session, including a retained historical run when a newer turn has started.
A missing session, unknown run or another owner's session returns 404. A malformed
run ID returns 400. No additional administrator requirement is added.

The response includes `session_id`, `run_id`, `status`, native `state`, `active`
and `source`. Running turns keep `status: streaming`; terminal states include
`done`, `error` and `stopped`. An error response includes `error`, the original
structured SSE error payload, including its message, `error_class`, retryability,
request-preparation flag and provider explanation when present. For example, an
intact input that exceeds the model's context window remains observable as
`status: error` with `error.error_class: context_length_exceeded` after the stream
has ended, even when no assistant message was saved.

Active `query`, `partial` and other existing stream fields remain available.
The generator records its run ID from the native causal context when creating
its active stream record. These fields are copied only while that recorded
identity and the current registry both match the selected run. A
replacement run's partial text is never attached to the old run's identity.
While replacement waits for its predecessor's cleanup or its queue, it does
not inherit the predecessor's query or partial response.
The existing start, stop, cancellation and idempotent-send paths are unchanged.

Status comes from the native in-memory registry, then the existing finished-run
marker and canonical run journal. Finished errors cache the exact error payload
in `finished_runs.json`, alongside its existing identity/status fields. Older
markers can recover the error from their existing journal. Eviction of the
in-memory replay buffer and a process restart do not erase that terminal result.
If both the error cache and its journal are unavailable, the recorded error state
can still be returned without inventing an error message. An unfinished journal
without a live registry entry reports `unknown`, not a live generation; status
reads do not perform startup recovery or change stored evidence.

The original `/api/chat/stream_status` and `/api/chat/resume` retain their active
stream contracts. This status endpoint is read-only and returns a snapshot; it
does not replay the SSE log to the client. A paused `waiting_user` or recovered
`interrupted` state likewise does not start more work.

The existing MCP `hoard_turn_stop` also accepts optional `run_id` and passes it
in the native `X-Odysseus-Run-Id` header. Older calls without that argument first
read status once, then use the observed identity for one native stop request.
A stale identity is never replaced by another lookup or another stop attempt.
If no identity is found, the native stop request still runs and returns its own
result. In particular, `scope: work` still performs the existing background-job
and process cleanup when the parent turn has ended. Stop scope and API policy
are unchanged; the facade does not invent a successful cancellation.
