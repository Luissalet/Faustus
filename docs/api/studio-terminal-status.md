# Studio terminal errors after refresh

Studio reads the existing owner-scoped `GET /api/hoard-copilot/turns/{session_id}/status`
when it hydrates saved history. A run can fail before an assistant message is
saved, so history alone cannot describe that result. The status response supplies
the canonical session ID, run ID and terminal error; unknown runs (404) leave the
ordinary history unchanged. Other status failures remain best-effort and do not
prevent history from opening.

For an inactive run in the `error` state, Studio decodes the native error with
the live SSE decoder and applies the same transcript reducer and completion
event. Its message, error class and trace fields therefore match the live error.
A matching saved assistant retains its text and database identity. Otherwise,
the screen adds a runtime error view with a stable run-derived UI identity and
no database ID or history index. It writes no assistant message, ledger or local
storage record. A missing error payload does not create a guessed error.

Runtime error views are excluded from both sides of `syncIds` matching. They
cannot shift saved-row alignment or acquire database IDs themselves. A rejected
new input that was never saved therefore cannot borrow the previous user's ID
or history index; ordinary saved messages still receive their own identities.

The same history hydration serves initial load, active-stream rejoin and
uncertain-send reconciliation. A terminal view does not become the current Stop
target. Active/queued runs still follow the existing rejoin and outbox flows;
outbox reconciliation retains its existing client message ID. Reading terminal
status itself never sends, resumes, retries or starts a turn.

History responses are applied only to their visible session and run epoch. A
late read cannot replace a new local turn, including one that has already
finished and cleared its controller. Session changes abort the old read.

`node studio/checks/studio-terminal-status.check.mjs` exercises the native GET
adapter, pure restoration and the actual React history/rejoin/outbox effects
with deferred responses. It covers fresh mounts, exact native errors, unknown
runs, owner failures, session changes and newer-turn races. Set
`QA881_BASELINE=1` to execute the original Studio hydration from the pinned
pre-change revision; its user-only history loses the terminal error.
`QA881_IDS_BASELINE=1` instead exercises the first 881 implementation before
the identity correction; its real `syncIds` callback incorrectly lends the
previous user's identity to an unpersisted new input.
