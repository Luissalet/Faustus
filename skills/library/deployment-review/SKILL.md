---
name: deployment-review
description: Runs the four-agent deployment review team (code, dependencies, CI, runtime logs) through `delegate_agents` and turns their findings into one merged Collaboration hub report with a correlated action list. Use when a release candidate needs a real go/no-go before it ships, not a single reviewer's opinion.
version: 1.0.0
category: planning
tags: [deployment, delegation, review, ci]
status: published
source: imported
---

## When to Use

A change is staged to deploy and you want more than one angle on it before
it ships: is the diff itself sound, are the dependencies clean, is CI
actually green (and if not, why), and does the runtime look healthy. Use
this instead of running one reviewer, because the value is in what two
agents independently notice about the *same* file or symptom.

## Procedure

1. Delegate the whole review to the coordinator in one call rather than
   wiring the four specialists yourself:

   ```json
   {
     "tasks": [{
       "agent": "deploy-lead",
       "instruction": "Review <change/branch/PR> for deployment to <environment>. Workspace: <path>. CI run: <run_id or 'latest failed'>. Log paths: <paths, if any>."
     }],
     "parallel": true
   }
   ```

   `deploy-lead` (`config/agents/library/deploy-lead.md`) is a
   `coordinator`: it fans out to `deploy-code-reviewer`,
   `deploy-dependency-auditor`, `deploy-ci-analyst` and
   `deploy-log-monitor` itself via its own `delegate_agents` call, so one
   task from you produces all four specialist passes plus the merge.
2. If you need only one angle (say, just the CI read), delegate to that
   single agent by name instead of the whole team — each of the four is a
   normal library agent on its own, usable without the coordinator.
3. Read the returned Collaboration hub in full before acting: the verdict
   line is a summary, not a substitute for the action list underneath it.
4. Hand the action list to whoever owns each item (a person, a follow-up
   `delegate_agents` task, or "block the deploy until fixed") — this skill
   produces the plan, it does not execute fixes.

## Pitfalls

- Delegating to the four specialists yourself instead of through
  `deploy-lead` loses the correlation step — four independent reports on
  the same file read as four findings instead of one confirmed one.
- Treating `deploy-ci-analyst`'s `propose: true` output as a diagnosis
  rather than a guess — it is a model's read of the log text, always
  labeled as such in its report, and still needs a human or a fix attempt
  to confirm.
- Running this with no CI run id and no failed run available: `ci_failures`
  will say so plainly rather than inventing a run; treat "no failed run
  found" as "CI is clean", not as a tool error.
- Skipping this for a change that "only touches config" — a dependency
  bump or a config value is exactly the kind of change the dependency
  auditor and the log monitor catch and a code-only review would miss.

## Verification

- The Collaboration hub names all four specialists explicitly, even when
  one of them found nothing (a clean report is still evidence it ran).
- At least one correlated finding is checked by hand against both
  specialists' raw reports before the action list is trusted, the first
  few times this is used, to confirm the coordinator is actually reading
  both reports rather than concatenating them.
- The action list's root causes are things a human can act on today (a
  file, a package, a job id) — not restatements of the verdict line.
