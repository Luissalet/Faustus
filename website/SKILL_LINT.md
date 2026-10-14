---
layout: default
---
# Skill writing checks

Faustus checks the complete saved SKILL.md, including its verification section,
when it learns or creates a skill. Findings are returned as `_lint_warnings`;
writing advice never prevents the skill from being saved. Extraction and instinct
evolution therefore report a created skill only after normal persistence.

Checks cover descriptions, compact procedures, references, and a concrete way to
verify completion. Spanish verification instructions such as `verificar` and
`comprobar` are recognised. Existing explicit verification is checked from the
canonical document rather than from a partial reconstruction.

Frontmatter `manual_only: true` survives reading and saving. Such skills remain
available in the skill library and explicit searches, but are omitted from the
automatic index and from both lexical and hybrid automatic selection. Set the
flag to `false` to make a skill eligible for automatic selection again.

Verification: the skill-writing, selector, frontmatter, instincts, governance,
open-format and owner-isolation tests cover advisory persistence, real extraction,
instinct evolution, manual selection and explicit access. Private native MCP and
browser checks exercise the regular skill API without using user data or an LLM.
