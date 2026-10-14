---
layout: default
---
# Code criteria

Project → Rules offers three app-wide choices for the bundled code criteria:
Original, Positive (short), and Off. Original is the default, following the
Sparks measurement recorded by Galton task 322. It contains four principles:
state assumptions, keep the solution small, change only relevant code, and
verify the requested result. Both texts are adapted for unattended work.

The choice is stored in `project_rules_karpathy_variant` through the shared
`/api/auth/settings` API. The existing `manage_settings` tool can read and
change it. It takes effect on the next rule projection without restarting
Faustus; the variant is part of the projection cache signature. The project
screen refreshes its “What the agent receives” preview after a change.

Bundled code criteria apply when the workspace has a detected programming
language. A chat without a code workspace, or a documents-only workspace,
receives neither variant. Only the selected bundled variant is projected.
Project-authored and explicitly installed rule files retain their existing
precedence and behavior. The selector changes the automatic library choice,
not the contents of a project's own files.

The selected criteria have priority 5 in the library, so they fit before the
generic entries under the usual context budget. A project's own instructions
still take precedence. The two variants describe code work; they introduce
no permission request, execution restriction, or extra approval step.

Verification covers the real default library budget, switching all three
choices, cache refresh, API persistence, code/documents-only workspaces,
the existing REST-to-MCP bridge, and reload of the normal project screen.
These integration checks do not measure model quality. Galton's benchmark
results apply to its recorded model, tasks, and prompts.
