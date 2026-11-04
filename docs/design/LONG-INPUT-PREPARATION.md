# Large input preparation

Workspace routing and path extraction inspect the complete supplied text.
They do not impose another input limit or shorten the request. Filename
matching now starts once per maximal filename-character run. The two routing
consumers use existence only, so a leading dash or dot can be included in the
match without changing their boolean result.

Path extraction retains its capture, ordering, de-duplication, file-mention
sigils, extension list and existing filters. The basename expression chooses
its first letter or underscore once. The previous two overlapping greedy
character runs tried every possible letter split when a long token contained
no filename extension. This blocked the event loop before generation.

`tests/test_long_input_preparation.py` covers filenames, Unicode, punctuation,
ordinary mathematics and path extraction. Its bounded child process compiles
the actual three expressions and checks 3.9 million characters with no filename,
a filename first, a filename last and a filename inside the same character run.
The original workspace matcher exceeded the twelve-second child allowance;
after that correction, the original path extractor independently exceeded it.
Both corrected expressions finish the same checks within that allowance.

This change composes with current-request preservation (867), long-message
transport (866) and linear URL removal (874). The isolated native API, MCP and
browser checks must exercise that composition. Pure CPU matching does not
certify model quality, transport preservation or production activation. Source
and native test receipts are kept separately; unfinished native checks remain
explicitly unfinished.
