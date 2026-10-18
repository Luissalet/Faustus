# Pasted plans and the human request

Importing a plan pasted into a human message records its tasks in the tracker
without replacing the message with only the first task. `replace_pasted`
returns the original text, including all sections, whitespace, Unicode, trailing
lines and any following attachment blocks. Its existing arguments remain
compatible; `max_task_chars` does not limit this human-authored message.

The tracker still parses and stores the plan, exposes task status, and supports
progress and continuation. Its brief describes the tracker and its current
task as a summary. It does not claim that the complete plan is absent or forbid
consulting other tasks. Individual tracked tasks do not replace requirements
elsewhere in the human request.

The agent loop already inserts a separate tracker brief when the message does
not contain an injected `=== Plan task` block. This change uses that existing
path without changing the loop, its settings, tool selection or continuation
policy. Availability of tracker tools in a particular tool menu is a separate
concern; naming a tool in the brief does not prove that it was offered or used.

Large plan attachments retain their existing path: their body can be replaced
with a tracker summary and the current task, while the full plan is stored for
retrieval. This change preserves pasted human text; it does not turn document
contents into human instructions or remove attachment budgets. Other context
processing and model limits also remain unchanged.

The regression tests exercise literal replacement, advancing tracker state,
following attachments, both brief languages and the real agent loop with a CPU
provider and blocked HTTP. These tests establish prompt preservation at that
seam, not exact downstream HTTP serialization or model quality.
