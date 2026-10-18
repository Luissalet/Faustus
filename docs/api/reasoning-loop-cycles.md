# Reasoning repetition cycles

The reasoning stream guard treats a repeated normalized sentence or line of at
least 60 characters as an anchor. Repeating a quotation in different drafts does
not by itself establish a loop. The complete sequences between the last three
prose anchors must match, including short nonempty units. Code-hinted anchors
retain the existing threshold of six occurrences and compare all five intervals.
Case and whitespace normalization and sentence/newline delimiters are unchanged.

For example, `anchor / checked one / anchor / checked two / anchor` is allowed;
`anchor / same step / anchor / same step / anchor` confirms repetition. Quotes
and code receive no blanket exemption. The recent intervals can establish a
cycle even if older occurrences had different intervening text.

Only internal comparison state is bounded: 4,000 normalized units and 64,000
normalized characters. On overflow the whole comparison window is forgotten;
discarded progress is never joined into an apparently identical interval. The
existing 4,000-character unfinished-unit buffer remains. A unit whose prefix was
discarded is skipped through its next delimiter and resets cycle history. A
complete unit exceeding that same buffer size is also a barrier, even if sent
in one provider chunk. These conservative storage rules can miss cycles longer
than the retained window; they do not truncate model input, output, saved text
or stream delivery and do not set a generation/token limit.

The raw-character, token, phrase, vocabulary and script guards still run first
and are unchanged. Content-channel checks do not use reasoning cycles. Error
classification still uses the existing `reasoning loop:` prefix. This change
does not alter recovery, sampling, model selection, Low retries or settings.

CPU regressions use the actual guard AST with default settings I/O isolated,
generic fixtures and multiple chunk sizes. They cover advancing quotations,
old/new drafts, short progress, consecutive prose cycles, the six-anchor code
threshold, oversized-unit barriers and bounded state. Existing pure guard and
historical paragraph tests are also replayed separately. No private reasoning
is included in the repository. Native API/MCP/browser and model validation are
separate checks; passing these CPU tests does not establish model reliability.
