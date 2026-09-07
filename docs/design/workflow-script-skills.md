# Script skills in project workflows

Production `skill` nodes support Python, JavaScript and Bash scripts in a
project's discovered skill folders, in addition to the existing `media:` recipes.
Instructions alone do not become executable commands: the node selects a script
and supplies an argument list. No command-string splitting or host fallback occurs.

Example source, at `.agents/skills/report/SKILL.md` inside the project workspace:

```markdown
---
name: report
description: Build the project report
version: 1.0.0
permissions_backends: [docker_workspace]
permissions_max_seconds: 60
outputs: [report=artifact:document]
---
Run scripts/report.py. Write final outputs directly to /artifacts.
```

Workflow node:

```json
{
  "id": "report",
  "type": "skill",
  "config": {
    "skill": "report",
    "version": "1.0.0",
    "script": "scripts/report.py",
    "args": ["--format", "markdown"]
  }
}
```

The workflow must belong to the project owner, and that project must have a
valid workspace. Docker and the configured Faustus sandbox image must already
be available; the workflow does not install them or fall back to the host.

## Permissions and source identity

The runner reads the skill's manifest, checks declared backend permissions,
and requests explicit approval for potential workspace modifications plus any
other permission the manifest requires. Every request binds the command,
workspace, image name, effective limits, run/node and source-content hash.
Changing a source file, dependency or argument while waiting invalidates it.

The executable source is copied from a bounded snapshot into a private run
directory under `/artifacts/.faustus-skill`. The container uses that copy, not
an instruction file reread after approval. A bundle permits 256 files and 256
directories, up to 16 MiB total. Links escaping the source folder are refused.
Discovery supports Git worktrees and rechecks the document size at load time.

Credential injection needs a separate explicit secret binding; this runner
refuses a secret-bearing manifest rather than obtaining ambient credentials.
It never treats discovery location as permission to read another project's files.

## Results and recovery

Successful outputs are collected into the artifact catalogue with owner,
project, workflow run, source hash and execution identity. Partial results are
retained on failure. `stdout`, `stderr`, truncation and artifact links are part
of the persisted node result, rather than disappearing when a script fails.

Execution uses the same conditional claims and effect tracking as other
workflows. Uncertain effects are not silently retried. Approval responses are
observed by the background worker without requiring an open browser tab.

Cancellation is checked throughout process execution against the exact worker,
attempt, lease and live workflow status. For cancellable scripts Docker creation
and start are separate operations: cancelling during setup prevents user code
from starting. Cleanup targets the returned container id, including a start
that races cancellation. No image is pulled implicitly (`--pull never`). A
cancelled script can have partial workspace effects; the effect ledger retains
that uncertainty instead of retrying the script. Cancellation of an already
submitted remote media job is a separate provider-specific operation.

Code: `src/workflows/skills.py`, `src/skills_runtime/discovery.py`,
`src/execution_router.py`, `src/execution_backends.py` and
`tests/test_workflow_script_skills.py`. Validation includes a real execution
using the installed container image and a generated Markdown artifact in
temporary test storage; no project source was executed during the test.
