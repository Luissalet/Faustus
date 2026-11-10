---
name: obsidian-markdown
description: Write and edit notes in the Markdown dialect Obsidian vaults use — [[wikilinks]] with headings, blocks and aliases, ![[embeds]], > [!callouts], YAML properties, #tags and nested tags, block ids, %%comments%% and ==highlights== — so the note links, renders and shows up in the graph. Use when writing or fixing notes for an Obsidian vault or for the Cerebro (Faustus's own vault), when the user mentions wikilinks, callouts, frontmatter, properties, embeds or tags, or asks for a note that must link into existing notes.
version: 1.0.0
category: writing
tags: [obsidian, markdown, notes, wikilinks, callouts, frontmatter, properties, tags, vault, cerebro, notas, boveda, enlaces]
status: published
source: imported
---

## When to Use

Any note that will live in a vault: the Cerebro (Faustus's markdown vault, written with the `brain` tool or the Cerebro screen) or a folder the user opens in Obsidian. Plain CommonMark/GFM (headings, lists, tables, code) is assumed; this skill covers only what the vault dialect adds and what breaks it. For `.canvas` boards see `json-canvas`.

Adapted from kepano/obsidian-skills, skill `obsidian-markdown` (MIT, © 2026 Steph Ango, pinned 3ccff5338ea7), rewritten for this library and for the Cerebro's rules. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Find the names first.** Before linking, search the vault (`brain` action `search`, or list the folder) for the exact titles of the notes you will point at. A link resolves by file name (the Cerebro also folds case and accents), so `[[Ollama]]` must match `Ollama.md`. A link to a note that does not exist is allowed and useful: it becomes an "unresolved" node that marks a note still to be written.
2. **Properties at the very top.** A YAML block between `---` lines, first thing in the file:
   ```yaml
   ---
   title: Despliegue en la Spark
   tags: [faustus, infra/sparks]
   aliases: [Despliegue Spark]
   created: 2026-11-10
   ---
   ```
   Lists use YAML lists; dates are ISO `YYYY-MM-DD`; `aliases` gives other names the link suggester knows; `cssclasses` styles one note. In the Cerebro, notes it mirrors (memories, entities, projects) carry `source:` and `kind:` — never add, change or delete those two keys by hand; a free note has no `source`.
3. **Link inside the vault with wikilinks, outside with Markdown links.**
   - `[[Note]]`, `[[Note|text shown]]`, `[[Note#Heading]]`, `[[Note#^block-id]]`, `[[#Heading in this note]]`.
   - External URLs: `[text](https://…)`. Never wrap a URL in `[[ ]]`.
   - A block id is `^id` at the end of a paragraph; for a list or quote, put it alone on the line after the block, with a blank line before the next paragraph.
4. **Embed with `!`.** `![[Note]]` (whole note), `![[Note#Section]]`, `![[diagram.png|400]]` (width), `![[paper.pdf#page=3]]`. An embed is also a link for the graph.
5. **Callouts for things to notice.** `> [!note]`, `> [!tip] Title`, `> [!warning]`, `> [!faq]-` (folded), `> [!todo]+` (open). Common types: note, abstract, info, todo, tip, success, question, warning, failure, danger, bug, example, quote. Every line of the callout body starts with `>`; nest by adding another `>`.
6. **Tags.** Inline `#tag` or `#area/subarea` (letters, digits, `_`, `-`, `/`; not only digits), or the `tags` property without `#`. A heading `# Title` is not a tag: the space makes the difference.
7. **The rest of the dialect.** `==highlight==`, `%%hidden comment%%` (invisible when reading; a block between two `%%` lines hides several lines), footnotes `text[^1]` with `[^1]: …` at the end, inline `^[footnote]`, math `$…$` and `$$…$$`, Mermaid in a ```` ```mermaid ```` block, tasks `- [ ]` / `- [x]`.
8. **Write the note in one pass**, then re-read it as the vault will: properties parse, every `[[link]]` names an existing note or is a deliberate new one, embeds point at files that exist, callouts have their `>` on every line.

## Pitfalls

- Square brackets, `|`, `#` and `^` inside a title break wikilinks: name files without them.
- A wikilink with a path (`[[Folder/Note]]`) works in Obsidian but the Cerebro resolves by title; prefer unique titles and plain `[[Note]]`.
- Tabs or a missing space after `:` in the properties make the whole block unreadable; the note then shows the YAML as text.
- Linking inside a `%%comment%%` still counts as a link for some readers; keep links out of comments.
- Editing the generated part of a mirrored Cerebro note (below the `%% faustus:generated … %%` marker) is overwritten on the next sync; write in the zone above it.
- Do not convert every mention into a link. Link the first useful mention of a note per section; a wall of `[[ ]]` hides the ones that matter.
- Callout type names are lowercase and English (`[!warning]`, not `[!Aviso]`); the title after it can be in any language.

## Verification

- The note opens with valid properties (or none) and its title matches the file name.
- In the Cerebro, the note's links appear in its panel and in the graph; unresolved ones are only the intended new notes (`GET /api/brain/unresolved` lists them).
- Tags show in the tag list (`GET /api/brain/tags`) with the expected nesting.
- Embedded files exist at the named paths; callouts render as boxes, not as quoted text.
