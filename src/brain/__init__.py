"""
src/brain — the second brain: a markdown vault Faustus owns, typed entities
with time, and an LLM-maintained wiki over everything it already remembers.

Nothing here is a new source of truth for what the assistant *knows*:
`memory_engine` still owns learned memories, `memory.json` the personal ones,
`services.objectives` the objectives and `project_concepts` the concept graph.
The brain adds three things on top of them:

- ``vault``    a folder of plain `.md` files (frontmatter + ``[[wikilinks]]``)
               mirroring those stores two ways, plus free notes whose source
               of truth IS the file. Readable and editable by a human.
- ``entities`` people, projects, tools, places… and the relations between
               them, each relation with a validity window, so "worked at X
               until March" and "works at Y" are both true, at different times.
- ``wiki``     short entity pages kept current by the cheap utility model,
               always citing the memories they were written from.

Everything derived lives in ``DATA_DIR/brain/brain.db`` and can be rebuilt.
"""
