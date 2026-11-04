# Announced actions and evidence outcomes

The agent continues a short final sentence that promises a concrete tool action
without making its call. A Spanish statement beginning with `sigo` only counts
when it names a continued concrete action, such as reading, checking, editing or
retrying. Cognitive statements such as `Sigo pensando que…` do not promise a tool
call. This lexical detector is conservative; it does not prove intent in every
possible phrasing.

The continued forms cover the concrete Spanish actions already recognized in
the present tense, including deleting, zooming, moving, importing and selecting,
plus downloading and the other supported inspection/repair actions. A prior
edit does not satisfy a later promised action. Regression coverage checks that
the promised second edit actually happens, rather than only matching a phrase.

Project tests still run against the turn checkpoint. A relevant test that failed
before the turn can still require repair when it exercises the requested change.
An unrelated pre-existing failure does not consume a repair round. Failure IDs,
fixed tests and baseline failures remain in the harness diagnostics.

As established by task 179, finishing the answer does not certify the work.
Known failing tests produce `unverified` and an evidence advisory; exclusively
pre-existing failures produce `uncertain`. The answer remains available. These
outcomes do not introduce another permission, rejection or completion gate.

Regression coverage exercises the real agent loop with a deterministic provider
and real pytest/checkpoint baselines. It distinguishes three rounds with one
repair attempt from two rounds with no repair for unrelated old failures, and
checks that the scorecard cannot report a false pass. Private API, MCP and Studio
QA checks transport and presentation with a synthetic HTTP provider. None of
these fixtures certifies model reasoning or semantic quality on new projects.
