# Background continuity

Autonomous work continues by default while Faustus is open. Browser heartbeats,
navigation and API reads no longer cancel every scheduled/manual task through
the optional foreground activity gate. Existing task Stop still cancels an
explicitly selected run, and all resource ownership, lease and duplicate-run
rules remain intact.

`BACKGROUND_TASK_FOREGROUND_GATE=true` explicitly enables the previous policy:
wait for a quiet UI, preempt background jobs when foreground requests or visible
browser heartbeats arrive. `false` is the new default when the variable is absent.
This is a scheduling preference, not a credential or execution permission.

The change applies to all existing native task/worker flows that use
`interactive_gate`; no new execution interface, model fallback or resource
allocation is introduced. Resource pools still prioritize foreground waiters.

Verification uses the actual task router, scheduler, SQLite and application
middleware/heartbeat functions, with a synthetic long builtin action and private
data. Execution MCP invokes the normal task API; opening/reading the browser and
heartbeats preserve `running`. Explicit Stop records `aborted`; a new manual run
completes once and records `success`. No production task/model is exercised.
The helper HTML is a private test control, not a replacement product interface.
The unchanged base reproduces `aborted` after run + heartbeat + API inspection,
before the synthetic action begins; the same operations on the candidate keep
the action running. The old outcome says `Stopped by user` even though no Stop
was sent; this incorrect legacy label is retained in the evidence.

145 area tests pass: foreground policy, request workload, scheduler leases,
task windows, period budgets and resource pools. Opt-in foreground tracking and
Stop remain available. This does not remove the scheduler's existing serial
model slot or claim universal parallel execution.
