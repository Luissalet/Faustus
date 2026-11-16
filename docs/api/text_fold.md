# Folding long text by level of detail

A long assistant answer or a long tool output can be read at five levels of
detail, in Studio and through the API and MCP, instead of always whole.

| Level | Name | How it is made |
| --- | --- | --- |
| 1 | full | The original text, never altered. |
| 2 | sentences | One to three literal sentences of each paragraph, chosen without a model (word frequency, with a preference for the first and last sentence). Headings stay; code and lists keep their first lines and say how many were left out. |
| 3 | key | The idea and the reason of each paragraph, written by the language model. |
| 4 | lines | One line per section, written by the language model. |
| 5 | topics | A map of a few topic cards, each naming its sections, written by the language model. |

Levels 1 and 2 are deterministic and always available. Levels 3-5 use the
global language model (the utility endpoint, which follows the default chat
model when unset), are generated the first time they are asked for and are
stored with the document. When the model does not answer, the request fails
with `fold.model_unavailable`; no stand-in is returned in place of a generated
level.

Every folded fragment names the paragraph or section it came from, so the
original can always be opened.

## Studio

A long answer (about 1,200 characters and at least three paragraphs) and a long
tool output show a level control: a slider, the numbers 1-5, and the keys
`1`-`5` while the block has focus. Each fragment has an **Original** link that
opens its paragraph or section in place. The chosen level is remembered for
that text.

## Open document format (`faustus.fold/1`)

```json
{
  "format": "faustus.fold/1",
  "id": "fold_<24 hex of the text's sha256>",
  "sha256": "...",
  "chars": 4924,
  "levels": {"1": "full", "2": "sentences", "3": "key", "4": "lines", "5": "topics"},
  "generated": [3, 4],
  "sections": [{"id": "s1", "title": "Results", "level": 2, "paragraphs": ["p1", "p2"], "line": "level-4 text"}],
  "paragraphs": [{"id": "p2", "section": "s1", "kind": "text", "start": 24, "end": 410,
                  "text": "original paragraph", "levels": {"2": "literal sentences", "3": "idea - reason"}}],
  "topics": [{"title": "Load test", "gist": "One sentence.", "sections": ["s1"]}]
}
```

`kind` is `text`, `heading`, `code`, `list` or `table`; `start`/`end` are
character offsets in the original text. The id is derived from the content, so
the same text always maps to the same document. Documents are kept per owner
for 30 days.

## HTTP

All routes are scoped to the caller. Errors are `{"error", "error_class"}` with
`fold.invalid` (400), `fold.not_found` (404) or `fold.model_unavailable` (503).

- `POST /api/fold` - body `{text}` or `{artifact_id, text_pointer?}` (a stored
  tool result; a JSON result folds on its longest string field unless
  `text_pointer` names one), plus optional `level`, `section`, `paragraph`.
  Returns `{document, view}`: the outline (sections, sizes per level) and, when
  a level or selector was given, that view.
- `GET /api/fold/{id}?level=&section=&paragraph=` - one level of a stored
  document: all of it, one section (`s1`...), or the original of one paragraph
  (`p1`...). The view lists `items` (each with its `paragraph` or `section`) and
  the joined `text`.
- `GET /api/fold/{id}/document` - the whole open document.

## MCP

The built-in `text_fold` server offers:

- `fold_text(text | artifact_id, text_pointer?, level?, section?)` - outline of
  the folded document and, optionally, one level.
- `fold_read(id | artifact_id | text, level, section?, paragraph?)` - one level
  of a document, one section, or the original of one paragraph.

When a tool result is too long for the prompt and is stored as an artifact, the
note left in its place points at `fold_read(artifact_id=..., level=2)` so the
rest can be read level by level instead of all at once.
