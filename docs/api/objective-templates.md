# Editable task briefs

The conversation objective editor offers four local starting briefs: opportunities
with evidence, a one-page offer, an editorial plan and a weekly summary. English
and Spanish are available. Select a template to preview it, then apply it to the
draft. Applying replaces statement/context/deliverable and preserves existing
native links. Only the normal Save commits the draft with its revision check;
closing the editor discards it. Replace bracketed project/date placeholders.

GET `/api/session/{session_id}/objective/templates?locale=en|es` reads the same
catalog, with normal conversation ownership. MCP `conversation_objective` action
`templates` takes session_id and optional locale. Read it and edit the returned
brief before the normal save/continue workflow. Discovery creates no objective,
run, file, schedule or message and invokes no model or external service.

Briefs require sources, observed dates, explicit missing data and an inspectable
saved result. They do not grant authority from reference documents. Recurrence
uses existing native automations linked from the objective; choosing a brief
does not enable one. These are original local templates informed by the four
deliverable categories of the supplied article, not copied paid-service prompts.
