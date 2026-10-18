# Request text in automatic code review

When automatic code review runs, its prompt includes the complete `user_text`
provided by the caller. Requirements after character 3000, Unicode text and the
final line are preserved. Previously this field silently retained only its first
3000 characters, so a later CLI or persistence requirement could be absent from
the review prompt.

The agent loop supplies the latest human message, excluding runtime continuation
notes. This change does not automatically retrieve an earlier project task or
combine requirements from previous turns. It also does not change the request
sent to the writing agent.

Automatic review remains disabled when `agent_auto_review` is `off`. Reviewer
selection, project overrides and the existing skip when `same` has only the
writer available remain unchanged. A new review context does not establish that
the reviewer uses different model weights or that its conclusions are correct.

The reviewer still receives the turn's diff and available test summary. Existing
diff grouping, size limits, grounding, timeouts, errors and skipped results are
unchanged. Sending the complete request does not guarantee that every diff is
complete or that an arbitrarily large prompt fits the model's context window.
Review findings are evidence to assess alongside functional tests, not proof of
full task acceptance or durable-storage correctness.

The focused regression tests use the real prompt builder and single-group
review path with an in-memory provider. They perform no HTTP or model calls and
do not test LLM accuracy:

```console
python -B -m unittest discover -s tests -p test_auto_review_request_contract.py -v
```
