---
name: obsidian-cli
description: Drive a running Obsidian app from the shell with the obsidian command (read, create, append, search, daily notes, properties, tasks, tags, backlinks, plugin reload and developer checks), and fall back to Faustus's own Cerebro tools when Obsidian is not open. Use when the user asks to act on their Obsidian vault from the command line, to develop or debug an Obsidian plugin or theme, or names the obsidian CLI.
version: 1.0.0
category: engineering
tags: [obsidian, cli, vault, notes, daily-notes, plugin-development, theme, terminal, cerebro, línea de comandos]
status: published
source: imported
---

## When to Use

The Obsidian CLI talks to an Obsidian app that is already running and acts on its vaults: useful when the user's real vault is open in Obsidian (not only the Cerebro), when a note must be created with an Obsidian template, and above all when developing a plugin or a theme (reload, read errors, take a screenshot, inspect the DOM). When Obsidian is not running the CLI cannot do anything; the Cerebro tools (the `brain` tool for reading, writing and searching notes, `brain_base`, `brain_canvas_export`) work on the vault files directly and need no app.

Adapted from kepano/obsidian-skills (MIT), skill obsidian-cli. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Check that it can work.** Run `obsidian help` (it lists every command of the installed version). If the command is missing or says no instance is running, tell the user and use the Cerebro tools instead for notes inside the Cerebro's vault.
2. **Pick the vault.** Commands act on the last focused vault; put `vault="Name"` first to choose another.
3. **Target files.** `file=<name>` resolves like a wikilink (no folder or extension needed); `path=<folder/note.md>` is exact. Without either the active file is used.
4. **Use parameters and flags.** Parameters take `=` and quotes when they contain spaces (`obsidian create name="Reunión" content="# Hola" template="Reunión" silent`); flags have no value (`silent`, `overwrite`, `total`, `--copy`). Multi-line content uses `\n`.
5. **Everyday commands.** `read file=…`, `append file=… content=…`, `search query=… limit=10`, `daily:read`, `daily:append content="- [ ] …"`, `property:set name=status value=done file=…`, `tasks daily todo`, `tags sort=count counts`, `backlinks file=…`.
6. **Plugin and theme loop.** After a code change: `obsidian plugin:reload id=<plugin>`, then `obsidian dev:errors` (fix and repeat until clean), then check visually with `obsidian dev:screenshot path=shot.png` (open the image yourself) or `obsidian dev:dom selector=".workspace-leaf" text`, and read `obsidian dev:console level=error`. `obsidian eval code="app.vault.getFiles().length"` runs JavaScript in the app; `dev:css selector=… prop=…` reads a computed style.
7. **Report** the command and what it returned; for plugin work, the error list before and after and the screenshot you looked at.

## Pitfalls

- Assuming the CLI edits files on its own: it needs the app open, and it acts on the app's vaults, not on arbitrary folders.
- Forgetting `silent` and opening dozens of notes in the user's window.
- Using `eval` for things a plain command does; keep it for inspection.
- Writing to the Cerebro's mirrored notes (memories, entities) through Obsidian: the Cerebro regenerates them; edit the user zone or use the Cerebro tools.

## Verification

- `obsidian help` ran and listed the commands used.
- Every write was read back (`read`, `backlinks`, or the Cerebro reindex) and matches what was intended.
- For plugin work: `dev:errors` is empty after the last reload and the screenshot shows the expected state.
