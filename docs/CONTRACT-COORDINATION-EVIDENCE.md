# Workspace evidence and remote coordination

A successful native coordination action remains an external effect receipt. It does not edit the current workspace, so CriteriaBook no longer advances last_mutation_seq, withdraws automatic proofs or clears already-attempted evidence links merely because an Agora message, checkpoint, heartbeat or task operation succeeded.

The exception applies to coordination_* calls classified by the turn ledger as effects. Explicit workspace mutations still expire earlier evidence, including a call classified as a mutation regardless of its name. Shell writes, Python writes with unknown paths and other unclassified effects retain their previous conservative behavior. Documentation-only handling also retains its existing behavior. This change does not alter coordination authority, permissions, tool execution or the receipt itself.

The start, audit and gap notes describe inferred criteria as advisory. They ask the agent to reuse suitable existing calls, retain pending states that the matcher cannot establish, and explain the limitation. They do not ask for successful external actions to be replayed solely to clear labels. Actual missing work and failed checks still need attention.

Strict matching, criterion states, summaries and complete_unverified diagnostics remain available and unchanged. An advisory note never turns a pending criterion into proven. For example, a prompt that concatenates an action name and thread number may produce a literal key that is absent from the response; that matcher limitation must be reported rather than satisfied with invented output or duplicate posts.

The issue was reproduced after six successful native catalog reads, a JSON selection and a verified Agora post. Treating the post as a workspace edit made earlier read receipts stale and prompted repeated queries. Regression coverage preserves both the read-after-coordination case and stale evidence after genuine file changes. End-to-end completion is validated separately with the normal native conversation and configured global model.
