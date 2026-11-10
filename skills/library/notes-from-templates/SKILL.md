---
name: notes-from-templates
description: Generate Markdown notes from a template and structured data — one note from a JSON object, or one note per CSV row, JSON array item or extracted web page — with frontmatter properties quoted correctly, file names made safe, a dry run before writing and no silent overwrites. Use when the user wants a batch of notes made from a spreadsheet or a list ("una nota por cada libro de este CSV"), a template applied to data, or clipped pages formatted the same way.
version: 1.0.0
category: writing
tags: [templates, markdown, notes, batch, csv, json, frontmatter, vault, cerebro, plantillas, notas en lote, knap]
status: published
source: imported
---

## When to Use

When the same shape of note must be filled many times — a reading list from a spreadsheet, one note per contact, one per meeting exported as JSON, articles clipped from several URLs — a template and the data produce consistent notes that Bases (`brain_base`) can then query. Faustus writes the notes itself; the user does not need a separate templating CLI.

Adapted from kepano/obsidian-skills (MIT), skill knap, as a method on Faustus's own tools. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Read the data.** CSV headers become field names (values are strings; convert numbers and dates on purpose), a JSON array gives one item per note, a folder of JSON files one note per file, a page from `reach_read` gives title, author, date and content.
2. **Write the template.** Frontmatter first: every property quoted and escaped as YAML needs (titles with colons, lists as YAML lists, dates as YYYY-MM-DD). Then the body with the fields in place and a conditional section only where a field may be empty. Keep wikilinks (`[[Autor]]`) where a field names something that has or should have its own note.
3. **Name the files.** One field (usually the title) made safe: no `/ \ : * ? " < > |`, trimmed, unique within the batch. Decide the folder.
4. **Dry run.** Render the first two or three notes and the full list of output paths; show them to the user and say which paths already exist. Duplicate names within the batch are an error to fix, not to number silently.
5. **Write.** With the `brain` tool for the Cerebro (or `write_file` in a folder the user named), skipping existing files unless the user asked to overwrite them.
6. **Check.** Read two notes back, and, if the user wants a view of them, create a base (`obsidian-bases` skill) and query it.

## Pitfalls

- Unquoted YAML: one title with a colon breaks the properties of that note.
- Treating every CSV value as text in a property that should be a number or a date, so Bases cannot sort or sum it.
- Overwriting notes the user edited by hand.
- Generating hundreds of notes without showing a sample first.

## Verification

- The dry run listed every path and flagged existing ones before anything was written.
- Two written notes parse (properties show in the Cerebro) and match the template.
- The count of written notes equals the number of data rows minus the skipped ones, and the skipped ones are listed.
