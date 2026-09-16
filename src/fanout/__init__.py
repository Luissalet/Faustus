"""src/fanout — R3 (Reach wave): "one prompt, N agents, each in its own
worktree, compare and merge the winner".

The pitch orca makes (INFORME §2, `stablyai/orca`): fan one prompt across
several coding agents, each isolated in its own git worktree, then let a
person diff and merge the winner by hand. This module does the same fan-out
but scores automatically instead of leaving the comparison to a human eye
(INFORME §3, OpenMontage's multi-dimension provider scorer: tests/harness/
cost/latency, weighted, with a logged reasoning) — and the N candidates need
not be the same model: a cheap local Qwen worker can run next to a paid
remote one and lose or win the round on its own results.

Reused rather than reinvented (rule 1 of the lot contract):
  * ``src.alternatives`` for isolation (worktree/snapshot_dir), the
    three-way ``git merge-file`` apply, and ``run_tests``.
  * ``src.agent_tools.subagent_tools._run_subagent`` for actually running a
    worker inside one candidate's isolated directory — the same mechanism
    ``delegate_agents`` uses for a task's workers, just one worker per
    candidate with its OWN model/endpoint instead of many workers sharing
    one.
  * ``src.budget_account`` for reserving/reconciling each candidate's token
    spend against the coordinator's run.
  * ``src.project_tests`` to auto-detect and run the target project's own
    test suite inside each candidate's isolated copy for scoring.

See ``docs/spec/fanout.md`` for the on-disk layout and the full contract.
"""
