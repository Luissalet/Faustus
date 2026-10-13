# Response quality in Studio

Responses lead with the answer, explain the evidence and distinguish reported
facts from interpretation and missing data. The common tool rules apply to native
function calling and the fenced-tool protocol. Exact requested formats still win.

Markdown links to an observed turn source become compact inline source links. The
original label and exact URL, including its query and fragment, remain. Matching
ignores the fragment only; another query, page or domain is an ordinary link.
Identification is not a verification verdict. Numeric references are not guessed
from position because repeated searches may restart numbering.

The sources disclosure keeps every observed source, with full titles and domains;
the expanded web activity also exposes the complete list. Persistence continues
using the existing `web_sources` metadata and normal API/MCP chat history.

## Native presentation fences

An `entity` (alias `faustus-entity`) fence takes a JSON object:

```json
{"title":"Subject","url":"https://example.com/subject","image":"/api/image/example","alt":"Subject image","body":"Markdown facts, with nearby links."}
```

Only `title` and `body` are required. The image uses the reader's normal image
handling. It retains proportions and alternative text; remote host consent and
existing image behavior are preserved. Use an actual returned image URL, not a
local filesystem path. The entity title remains visible even when it is a source.

A `details` (alias `faustus-details`) fence takes:

```json
{"summary":"Method and limitations","body":"Supplementary Markdown, tables, images, charts or footnotes."}
```

Both fields are required. Content is rendered through the ordinary Markdown
reader, preserving footnotes and workspace links. It never interprets raw HTML.
Malformed or unfinished JSON remains visible as code during streaming and after
reload. Keep the main conclusion and material uncertainty outside the disclosure.

Existing charts, images, output attachments, choices, decisions and activity
groups remain available. A chart needs real labels/values/units and a source
caption immediately below; a chart renderer does not establish data quality.

## Verification and scope

`node studio/checks/response-quality.check.mjs` mounts the actual React reader
and checks source matching, nine-source retention, query distinctions, entity
titles/images, details, footnotes and invalid input. `tests/test_response_quality.py`
also checks both prompt protocols. Desktop/mobile inspection and native persisted
chat/API/MCP checks are recorded in the delivery evidence.

These native layouts belong to Studio's reader. The embedded Hoard panel and
legacy chat have separate readers; their Markdown links remain usable but native
entity/details layout parity is not implied. Autonomous model quality must be
assessed with actual model-backed cases, separately from scripted rendering.
