# Query original source text in stored results

read_artifact accepts text_pointer, an RFC6901 pointer selecting a literal string inside the stored JSON result. For example, text_pointer: /full_text selects the source returned by Reach instead of searching a serialized preview plus its duplicate. Use it with query, occurrence and context_chars to retrieve a passage in one call, or with start/end to read a range. An empty pointer selects a root string. The selected string is not decoded as JSON again.

Offsets and total_chars refer to the selected source string. Query matches additionally report line (the match's 1-based source line) and context_start_line. Range results report start_line. Lines count LF separators, including CRLF; original text, CRLF and Unicode characters are retained. The result identifies text_pointer explicitly. An absent occurrence still returns an empty matches list.

The stored artifact is resolved for its owner before pointer or query validation. Missing/non-string selections and malformed pointers return explicit errors. This mode cannot be combined with JSON selection. Existing JSON filters, raw query/range response shapes and ownership behavior remain unchanged when text_pointer is absent. The range/query algorithm is shared with the existing authorized reader; the pure slicing function does not access storage.

The normal catalogue, offload recovery note and artifact_search hints now prefer direct context queries for a known artifact. Source results with full_text advertise that pointer. Search across artifacts and offset ranges remain available. No new tool, permission, service or background process is introduced.

## Evidence and remaining work

Silver1760 follows native1755: 57 tool calls, including thirteen searches and nine artifact reads, with several search-offset-read sequences. That run produced useful Route203 tables but took774.05 seconds of CLI time and corrected a false double-battle description after its first post. This feature addresses source selection and the repeated retrieval steps; it does not establish general model efficiency or factual correctness by itself.

A fourteen-method external contract was frozen before implementation. It covers source coordinates versus duplicated preview, CRLF, Unicode expansion, nested pointers, literal root strings, missing/non-string selections, ownership-first validation, old JSON/query shapes and the real catalogue. The corrected implementation passes all fourteen. A new native task must still show whether the configured model uses it effectively. Its outcome is recorded separately from fixture and backend timings.
