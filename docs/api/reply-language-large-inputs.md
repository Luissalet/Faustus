# Reply-language URL scanning on large inputs

Reply-language detection removes URLs from the user's instruction text before
scoring its language. The scanner keeps the earlier pattern's exact behavior:
an ASCII-style scheme with Python's Unicode IGNORECASE semantics, or `www.` at
a Unicode word boundary, followed by at least one non-whitespace character.
The rest of that token, including trailing punctuation, is removed. A URL is
replaced with one space; surrounding text is unchanged.

The former greedy scheme search retried a long suffix at each word boundary
in hyphenated input such as `current-data-current-data-...`. That made the
preparation of large requests quadratic, before the model could receive them.

The scanner now visits each non-whitespace token and its maximal scheme
character runs. Each run has only one possible trailing `://` delimiter.
The first valid scheme start or `www.` start removes the rest of the token.
Each character is scanned a fixed number of times, including when a URL is
present at the beginning or end of the input. The regex classes retain the
previous Unicode behavior, including `İ`, `ı`, `ſ`, `K`, and word boundaries
next to non-ASCII characters.

Attachment-body removal, media chrome, visible text extraction, language
scoring and language directives retain their existing behavior. No message
text is changed before sending; URL removal applies only to instruction
analysis. Existing HTTP and MCP turn operations use the same detector and
need no new operation or parameter.

Validation compares the old pattern with the new scanner on 32,768 exhaustive
short strings, 10,000 fixed-seed generated cases and explicit Unicode,
boundary, whitespace, punctuation and attachment examples. A subprocess
regression checks four inputs of at least 3.9 million characters: no URL,
URL first, URL last and many delimiters. Run the area entry point with:

```text
python -m pytest tests/test_reply_language.py
```

This correction does not establish a model context limit or remove other
large-input bottlenecks. An independent native test found a later quadratic
filename search in `agent_loop._WORKSPACE_CODE_TARGET_RE`; that is a separate
issue. CPU-provider tests of this URL correction do not demonstrate model
quality or completion of the later issue.
