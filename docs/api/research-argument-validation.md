# Research tool argument types

`manage_research` rejects non-string values for `action`, `id`, `session_id`,
`research_id` and `search` before accessing saved reports. It returns
`error_code: invalid_arguments`, `exit_code: 1` and the offending field name.
Lists, objects, booleans and numbers are not coerced into actions or report IDs.
The user can correct the argument in the next normal chat turn.

Existing default listing for omitted, empty or null optional values is preserved.
Valid list, read and delete operations retain their existing permissions and
effects. This validation does not change approval policy, ownership handling or
the behavior of unknown textual actions and malformed JSON.

## Verification scope

The area suite passed 52 tests, including invalid types that must not access the
report directory, a failed delete followed by a corrected read, unchanged report
bytes and the valid delete of a disposable fixture. The native chat API returned
the argument error on the first turn and actual report contents after a corrected
second turn. Persisted sessions, MCP session events/usage and Studio were checked
against isolated data with a deterministic provider. This is not a model grade.

Evidence: `D:/LocalAI/tempfiles/qa-research82/`.
