# File descriptions in large requests

The deliverable detector accepts the existing Spanish and English write requests
and file descriptions such as `report.md con detalles` or `report.csv with totals`.
It keeps the same extension list, Unicode word boundaries, case handling, greedy
path selection, ordering, and case-insensitive basename deduplication.

Previously, the description matcher tried a complete path again at every word
boundary. A request containing `current-data-` repeated 300,000 times (3,900,000
characters) kept the request thread busy before the first provider request.

The description matcher now scans each maximal path run once. It finds candidate
extension endings and checks the existing description suffix at each ending. The
suffix check is bounded to 40 characters. The earliest word boundary and the last
eligible ending preserve the former leftmost search and greedy path semantics.
The next run starts after the complete previous match, preserving non-overlap.
The write-verb matcher and all other deliverable behavior remain unchanged.

`tests/test_deliverables_large_input.py` compares both full extracted paths and
the public basename results against the historical expression across 31,920
boundary/extension combinations and 20,000 seeded mixed requests. A subprocess
also checks the original 3.9-million-character input, URLs at either end, a real
file description before or after it, and a long valid path. No LLM is needed.

This fixes preparation of the input. It does not increase a model's context
window or establish model output quality. The existing context check still
decides whether the intact request fits the selected model.
