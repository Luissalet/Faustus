# Vocabulary collapse in model streams

The existing stream guard also recognizes a sustained collapse into one dominant word separated by changing fragments. This covers word salad that never repeats one exact phrase and stays in the expected writing system. Detection reads a bounded raw-text window so provider chunk boundaries do not split the words used for this check. It requires both dominance and low vocabulary diversity; numbered records, JSON keys and single-letter code/math tokens are excluded from this additional check.

A match uses the existing `DegenerateOutput` error and native recovery path. It adds no generation/token/time limit, permission prompt, recovery attempt or change to the sampler configuration. Stop remains terminal. It does not establish the physical cause of a model failure or guarantee semantic answer quality, and it does not restart the Sparks service.

The reported cancelled GLM response contained almost 8,000 characters of incoherent text upstream in its recorded provider trace. Its later user instruction was still queued and had not been processed. The new detector recognizes that recorded failure after about 1,163 characters with one-character chunks; eight coherent responses from the same conversations remain accepted. Private regression checks use synthetic text, not committed user conversations.

Verification includes stream/recovery unit tests and a private native API/MCP/UI fixture that forwards the actual guard error through the existing group bridge. No user message was replayed and no model was loaded for these checks. Four wall-clock ceiling tests fail identically on the untouched baseline; they are reported separately rather than counted as passing.
