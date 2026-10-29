# Read later occurrences in stored results

The native read_artifact tool accepts occurrence (positive integer, 1-based, default 1) and context_chars (non-negative integer, default 400) with a non-empty query. Matching remains case-insensitive and skips non-overlapping occurrences. For a prototype, call and definition of the same symbol, occurrence: 3 reads the definition; context_chars: 1500 can expose the surrounding function in one request. Offsets and context boundaries refer to the exact stored text, including when Unicode lowercasing expands a character. Stored text may be a serialized tool result.

An absent occurrence returns an empty matches list. The default query result shape, range reads and JSON selectors remain unchanged. Query options cannot be combined with JSON selection or used without a non-empty query. Invalid option types return an argument error, including booleans or numeric strings. Existing owner resolution and not-found behavior are retained; ownership is checked before query arguments.

This extends the tool in the product and canonical model catalogue. It does not automatically choose the definition, interpret source code, or establish any performance factor or billing reduction. Native evaluation still has to check whether the configured model uses the capability efficiently.

## Native result1750

The configured Sparks model used occurrence/context queries on the fixed Platinum source. The reader itself worked, including later occurrences and larger context. The complete run still took577.37seconds,46tool calls and29rounds, including22generic fetches, guessed paths and an unsupported directory read. Its final report claimed14fetches and inferred no money loss/blackout without proving those negatives; Root rejected those claims. The speciesproc compilation join was not completed independently. This is a positive reader test and an unsatisfactory efficiency result, not a completed research guarantee. Task1755 addresses the demonstrated source-reader gap and is evaluated separately.
