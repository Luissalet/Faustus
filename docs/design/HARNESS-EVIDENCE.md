# Evidence in final reports

The agent compares its report with observed tool results and records warnings. Ordinary evidence checks do not reject or replace the model's answer, ask for another approval, or make another model call solely to prove that answer. The normal chat API, persisted chat metadata and REST MCP bridge carry the same diagnostics.

## Files and test data

Paths in a described test, fixture or example are distinguished from workspace deliverables. This is a contextual classification, not evidence that the test passed or that the data exists. Explicit user targets, test source files and direct artifact claims remain checked. The classifier uses the described scope and intent rather than exempting filenames, formats, all test commands or all files named in a test paragraph. Separate claims before or after a fixture description remain checked.

A report of a pytest case that creates input files in `tmp_path` therefore does not assert that those files were delivered in the project folder. A claim that the requested export was saved there still needs observed evidence. Path recognition is lexical and covers source and common artifact formats; it is not a semantic verifier of every possible claim.

## Warnings and grades

`harness_check` events with `status: advisory` describe unsupported paths, unconsulted sources, factual inconsistencies and unavailable evidence. An unsupported file claim adds a brief evidence notice while preserving the answer. `claim_check`, `claim_diagnostics`, `answer_diagnostics` and `verification` persist in the normal harness metadata so a reload preserves the distinction between reported, observed and unverified work.

Ending generation does not certify success. The existing outcome classification and changeset receipts retain `unverified`, `uncertain`, failed tests and unsupported claims. The legacy `completion_gate` helper is used to grade the evidence (`complete_unverified`), not to censor a final answer or force an extra model call. An actual unsupported result must not become a verified completion merely because its prose was kept.

Scorecard quality flags follow the persisted verification outcome and advisory evidence notes, including older summaries without a verification section. A completed report with unverified or uncertain evidence is counted as unverified; it does not increase the verified rate. Deterministic calculation and saved-link discrepancies record an evidence notice in the summary notes with the checked result when available. They do not append automatic text to the model's answer. Studio preserves the emitted verification grade in live events and restored history. An explicit `unverified` grade opens the existing Unverified summary and shows its notes; a generic advisory without a grade does not invent that outcome. `uncertain` is not promoted to verified by a changeset receipt. Older summaries without a grade retain their existing checks/changeset presentation. Historical completion-gated checks remain readable.

The automatic fresh-context final verifier and final advisor do not spend another model call to accept the report. Their absence is explicit in diagnostics; no independent model verdict is invented. Other useful execution remains available: requested steps, configured work cycles, recovery from a real tool error, and repairs backed by actual syntax/test failures. Plan coverage continues requested unfinished work. Completion-engine proposals based only on missing proof or unsupported narration are retained as advisories; native candidate metadata distinguishes them from real failures and requested work. Older decisions without candidate metadata keep the planner's existing behaviour.

This change does not alter tool permissions, ownership, trusted workspaces, security checks, approval settings or destructive-action safeguards. Existing transport/rendering normalisations, such as using the real URL of a generated image, remain separate from evidence censorship. Configured code review and actual failure-repair workflows remain separate from a final prose verifier.

## Validation and limits

Regression tests distinguish temporary test data from a missing deliverable and keep unsupported work unverified without rejection rounds or response replacement. Isolated native QA uses a normal HTTP model provider, runs a real pytest case with temporary files, and exercises chat API, REST MCP stdio and Studio history, without GPU inference or real user data. This checks the harness and transports, not an autonomous model's code quality.

The REST MCP bridge has an existing 20 KB response limit. A large SSE result can therefore deliver the answer and early evidence warning while truncating later metrics. That response is labelled partial in QA; API/history and Studio verify the persisted full diagnostics. No complete-delivery receipt is inferred from a truncated response. Full tool access in the isolated QA instance uses the existing approval setting and does not imply host filesystem confinement.
