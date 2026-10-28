# Read selected JSON from a stored tool result

The native read_artifact tool can select complete JSON values without paging through character offsets. Existing range and substring reads retain their behavior and use the same owner-scoped artifact lookup.

Use json_pointer (RFC 6901) to select a value. An empty pointer selects the root. Escape / as ~1 and ~ as ~0. Array indices are zero based. A missing selected path or malformed JSON produces an explicit tool error.

For an offloaded tool result whose stdout contains a JSON string, set json_source to /stdout first. That field is decoded once, then json_pointer applies inside it. Object-valued sources are selected directly; there is no implicit recursive decoding.

To keep selected array items, supply both match_pointer and match_value. The pointer applies within each item. The match value is a JSON scalar: strings, numbers, booleans or null. Numbers and booleans remain distinct; a missing field does not match null. Items retain their full content, order and duplicates. No percentages or other values are combined.

Example arguments:

    {"artifact_id":"occ_example","json_source":"/stdout","json_pointer":"/entries","match_pointer":"/reference/id","match_value":7}

The output JSON contains the artifact reference, selected value, total_items before matching, matched_items after matching and total_chars of the stored result. Counts are null for a non-array value. Zero matches returns an empty array, which describes only that stored result and does not certify real-world absence. Large selected values still use normal result offloading; this is selection, not pagination or a global token limit.

Do not combine JSON selection with start, end or query. Read the catalog response schema rather than interpreting positional integers as unrelated entity identifiers. The feature does not modify or copy the stored artifact and does not execute expressions or code.
