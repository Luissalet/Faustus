# Durable change evidence

Implemented locally, 7 September 2026.

## What is saved

`src/changeset_store.py` stores immutable receipts in `DATA_DIR/changesets.sqlite3`.
A receipt contains the complete ChangeSet, original proof and rendered report,
owner/project/run scope, completion source, fingerprint and storage timestamp.
SQLite transactions serialize concurrent writers; duplicate identical writes are
idempotent, but reusing an identifier to overwrite different evidence is refused.
Records are bounded to 2 MB. SHA-256 catches accidental corruption; it is not a
signature against somebody with control of the database. Unknown schema versions
are refused without migration or overwriting.

The producers are actual execution boundaries:

- `src/agent_loop.py`: after building the final ledger-derived report, before
  streaming its summary. The write runs outside the event loop. Incognito turns
  never write; an unavailable store does not abort the answer and is shown in its
  summary. Chat run references use the session id, with a distinct receipt id for
  every turn and the server-resolved project/owner.
- `src/dispatch.py`: after the job is settled and its terminal state persisted.
  The receipt uses the actual nested `compact(job).result` evidence and preserves
  the job's proof, including worker cancellations and external-runner uncertainty.
  The existing proof-off setting remains respected. Dispatch without an explicit
  project binding is indexed by owner/workspace/run, not assigned an invented
  project.

Saving a receipt does **not** certify success. `verified` requires completed work,
the `proved` verdict, successful executed verification, a content-exact change list
and no inconclusive/pre-existing-only result, failures or unsupported claims.
Nonverified receipts are retained too. Successful tools or a checkpoint alone do
not get the UI's “Verified” heading.

## Reading and integration

- `GET /api/changesets/receipts`: exact owner filtering, optional project,
  workspace, run and verified-only filters. Descending keyset pagination,
  `limit=1..200`, `before=<cursor>`; summaries only.
- `GET /api/changesets/receipts/{id}`: original full receipt. Another owner's id
  gives 404. Admin session / owner-bound `agents:dispatch` API-token checks match
  the dispatch routes. No public write/import endpoint exists.
- The turn summary's **Read saved evidence** control fetches the original report
  on demand, with English/Spanish labels, retry, a bounded request timeout and
  cancellation on close/unmount. Legacy turns without a stored receipt do not
  get a misleading link.
- `WorkspaceAdapter` fills `project_state.v1.last_verified_changeset` from the
  exact owner/project/workspace scope. This read is separate from the shared Git
  cache, and real receipts never enter simulation/branch namespaces.
- `/build` and `/from-dispatch` stay nonmutating previews. They do not turn a
  caller's supplied `ok: true` into durable verified evidence.

The receipt is a snapshot of **evidence**, not a frozen checkout. A requested
file diff still compares its base checkpoint with the current workspace and is
labelled as such. `services/review_state.py` remains authoritative for a person's
accept/reject decisions; its checkpoint reference and the report's base refer to
the same original workspace state but have different purposes. Delta comparison
previews are not automatically imported into this execution history.

## Verification

`tests/test_changeset_receipts.py` covers reopen/restart equivalence, immutable ids,
32 concurrent writers plus duplicate retries, owner isolation, pagination, schema
refusal, corruption, size limits, incognito, unavailable storage, native dispatch
envelopes, preserved worker uncertainty, HTTP permissions and State Mirror scope.
`tests/test_studio_saved_evidence_js.py` runs actual TypeScript adapter/React
rendering checks and pins request cancellation, safe text rendering and recovery.

Combined focused suite: **237 passed**, `logs/astra-receipts-focused.xml`.
TypeScript, Spanish translation generation/check and the production build pass.
The existing large-bundle warning remains a separate optimization issue.

Real local-model browser check: Qwen 3.5 9B created and read
`D:\LocalAI\qa-objectives-20260907\receipt-qa-20260907.txt` in the synthetic QA
project. Receipt `chg_ba6596fe974a49e7873f` opened from the actual turn summary;
its original report correctly says `partial`, no verification runner, one change.
This is not presented as a test suite passing for that synthetic file.

The first reload exposed an existing final-metrics projection bug: `changeset`
and `round_count` were absent from the saved harness fields. Both are now retained;
an executed AST projection test and a real TypeScript history-restoration test
pin this boundary. Old messages already saved without the reference are not
rewritten or silently matched to a potentially different receipt.

A second real Qwen turn created and read `receipt-qa-reload-20260907.txt`.
After reloading the browser, receipt `chg_e5b448850664477a802b` still opened
the original report, with all three rounds restored. The English request was
answered in English. This also exposed concatenated paragraphs across a tool
round: the separator existed in the private accumulator but not in streamed
deltas. The normal tool-round boundary now emits that separator, covered by
an actual agent-stream regression. A later focused pass covering this, receipt
restoration and Codex event bounds reports **80 passed** in
`logs/astra-receipts-stream-bounds.xml`. The new stream separator has not yet
been rechecked in the running browser build.
