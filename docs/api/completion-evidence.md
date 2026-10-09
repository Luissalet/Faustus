# Completion evidence

The native chat API and its MCP bridge use the same per-turn `TurnLedger`.
A successful write receipt backs a completion claim; a failed, blocked or
approval-pending call does not. File claims still require evidence for their
individual paths.

A requested summary or explicitly attributed report may describe a historical
state such as “according to the author, the parser regression is now fixed”.
The ledger excludes that sentence from the current-turn edit claim scan only
when a successful read contains the same state, every named path and at least
two identifying subject words. Structured JSON records are decoded as data. Receipts are bounded
to 200 fragments of at most 1,000 characters and are not carried into later turns.

First-person changes, possessive claims such as “my changes” and explicit
“in this turn” completion remain claims of this turn, even within a summary and
in read-only mode. A request to make changes does not gain the summary exception.
Unsupported states, unrelated source content and negated historical states
remain subject to the ordinary completion gate. This lexical check distinguishes
attribution; it does not independently certify the truth of an external report.
Read content never grants tool permissions or executes instructions.

The exact native coordination writes `coordination_heartbeat`, `coordination_add`,
`coordination_claim`, `coordination_checkpoint`, `coordination_post`,
`coordination_submit`, `coordination_review`, `coordination_done`,
`coordination_lock` and `coordination_unlock` count as non-file effects on success.
The exact reads `coordination_poll`, `coordination_detail`, `coordination_task`
and `coordination_board` ground observed paths and structured external sources.
They do not count as opening a web page. Unknown `coordination_*` names gain
neither classification. Connector authority, scope, locks, revisions,
read-only restrictions and injection guards are enforced separately.
