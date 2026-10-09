# Markdown document lint

A report-only linter for Markdown documents. It never rewrites a file: every
finding is `{rule, severity, line, message, fix}` and the fix is a hint.

Code: `src/doc_lint.py`, `src/agent_tools/doc_lint_tool.py`,
`routes/doc_lint_routes.py`, the `doc_lint` tool of `mcp_servers/harness_server.py`.
Config: `config/doc_lint.json`. Tests: `tests/test_doc_lint.py`.

## Rules

| Group | Rule id | Severity | What it flags |
|---|---|---|---|
| headings | `heading-skip` | warning | a heading more than one level deeper than the previous one |
| headings | `heading-multiple-h1` | warning | a second H1 |
| headings | `heading-missing-h1` | info | no H1 (or no headings) |
| headings | `heading-duplicate` | warning | same text, same level, same parent section |
| numbering | `numbering-gap` | warning | `## 4.` after `## 2.` (lists what is missing) |
| numbering | `numbering-duplicate` | error | a section number used twice |
| numbering | `numbering-order` | error | a number lower than the one before it |
| numbering | `numbering-orphan` | warning | `3.2` with no section `3` before it |
| links | `link-broken` | error / warning | a relative Markdown link (or reference definition) whose target does not exist; a target outside the repo root is reported, never probed |
| links | `path-missing` | warning | a backticked repo path that does not exist |
| dates | `date-impossible` | error | `31-02-2026`, month 13, year outside the configured range |
| dates | `date-malformed` | warning | `9-10-2026`, `2026-1-5` (not zero-padded) |
| sections | `required-section` | error | a heading the document must have (per document, from the config) |
| forbidden | `forbidden-content` | error | a configured regex or banned name (defaults: commit trailers; the names list is empty) |
| whitespace | `trailing-whitespace`, `tabs` | info | off by default |

Numbered sections understand `## 12. Title`, `### 3.4 Title` and `## §12 Title`;
unnumbered headings in between are ignored and the children of a section are
renumbered from scratch under each new parent.

Fenced code blocks are skipped by every rule except `forbidden-content` and the
whitespace rules. A backticked string counts as a repo path only when it has a
slash, is ASCII without glob or prose characters, its first segment is a real
top-level entry of the repo, and it has a file extension or a trailing slash.
`path_ignore` globs (default `data/*`, `logs/*`, `venv/*`, ...) are skipped.
Links and paths resolve against the document's repo root (the nearest ancestor
with a `.git`).

## Config

`config/doc_lint.json` (or `.doc_lint.json` in another repo) is merged over the
built-in defaults; a broken file is ignored.

```json
{
  "disabled_rules": [],
  "whitespace": false,
  "date_year_range": [1990, 2100],
  "path_ignore": ["data/*"],
  "documents": {"README.md": {"required_sections": ["Quick start"]}},
  "forbidden": {"patterns": ["Co-Authored-By\\s*:"], "names": []}
}
```

`disabled_rules` takes rule ids or group names. `documents` keys are a repo
relative path or a file name, case-insensitive; a required section matches a
heading that contains the text (numbering ignored).

## CLI

```
python -m src.doc_lint README.md FAUSTUS.md [--json] [--rules headings,dates]
                       [--config FILE] [--fail-on error|warning|info|never] [--list-rules]
```

Exit code 0 clean, 1 findings at or above `--fail-on` (default `error`), 2 bad
arguments or an unreadable file.

## Agent tool

`doc_lint {paths: [..], rules?: [..]}`, read-only. Paths go through the same
workspace guard as the file tools; a path outside the workspace is refused per
file and not read. At most 20 paths per call.

## REST

Admin only.

- `GET /api/doc-lint?paths=README.md,FAUSTUS.md&rules=headings,dates`
- `POST /api/doc-lint` with `{"paths": [...], "rules": [...]}`
- `GET /api/doc-lint/rules` - the catalogue

Response: `{files: [{path, findings, counts} | {path, error}], summary: {error, warning, info, total, files, errors_reading}, rules}`.
A path outside the allowed roots answers `403` and nothing is linted; an unknown
rule or a missing `paths` answers `400`.

## MCP

The built-in harness server (`mcp_servers/harness_server.py`) exposes `doc_lint`
with the same arguments. Its paths must be inside the Faustus install folder or
the server's working folder.

## Limits

Heuristics, not a parser: only ATX headings (`# Title`) are read, `path-missing`
skips paths whose first segment is not a top-level repo entry, and a link
anchor (`#section`) is not checked against the headings.
