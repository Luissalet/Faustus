# Library Knowledge

Library contains a Knowledge section with the existing Memory and Brain
workspaces. Memory keeps facts, learned rules, decisions, leases, provenance
and change history. Brain keeps the Markdown vault, notes, graph, tags,
search, trash and synchronisation.

Open Library > Knowledge, then choose Memory or Brain. Both screens keep
the Library section navigation. The contextual return link opens Knowledge.
Chat actions retain the Memory shortcut. The palette still finds Memory
and Brain by their original names.

The original `/memory` and `/brain` routes remain valid, including Memory's
`t=provenance`/`changes` and Brain's `note`/`view` parameters and fragments.
`/library?type=knowledge` opens Memory inside Knowledge. No stores,
identifiers, API contracts or MCP tools are migrated or renamed.

`node studio/checks/library-knowledge.check.mjs` runs the actual components
through Library, Memory, Changes, Brain and return navigation with stored
synthetic content. It checks Spanish labels, legacy query/hash preservation
and that navigation makes only GET requests. Existing Brain checks,
TypeScript and the Vite build cover the shared route change. Browser DOM/AX
checks cover desktop and 390px mobile. Screenshot capture was unavailable
during the author's QA; pixel inspection is not claimed.
