# Turn preparation and availability

The chat route and agent loop await their existing pure math and intent
predicates through `src.async_preparation.run_preparation`. The complete
request and routing arguments reach the same predicates; their results and
exceptions are preserved. No request-size rule changes routing or removes text.

The same worker ownership applies to the pure tool-policy and thinking-mode
builders, keyword hints, sampling/admin/small-talk predicates and construction
of the private per-turn evidence ledger. These builders return new local data;
model calls, tools, writes and existing ledger updates stay in their original
execution paths. The ledger is handed to the loop only after construction ends.

The helper uses `asyncio.to_thread`, including its contextvars propagation.
Only preparation belongs here: never model calls, tools or shared mutations.
Builders may own a private payload exclusively until the await finishes. The
streaming small-talk policy still changes that payload exactly as before; its
policy function and the actual transport remain unchanged.
The route's context preparation also owns its route cache exclusively while
awaited. The budget/trimming function remains unchanged and its original callers
receive the completed result before observing that cache or dispatching.
The worker belongs to its awaiting task. Cancellation waits for that worker to
finish before propagating, even when cancellation is repeated; it does not
leave an unobserved worker behind. Agent preparation also polls the existing
`pending_cancel` callback before and after each predicate so Stop cannot advance
to another stage or model dispatch. Existing cancellation polling errors retain
the agent loop's behavior.

This improves event-loop availability between regex searches, without making
Python regex release the GIL during an individual search. It does not promise
instant interruption of CPU work or a fixed latency for arbitrary inputs.

The causal CPU fixture is 3,900,000 characters (`current-data-` repeated
300,000 times). Before offload, its math, agent-intent and tool-intent predicates
blocked a 20 ms heartbeat for approximately 1.7, 7.0 and 9.5 seconds. The same
predicates in workers retained identical results and reduced the largest gaps
to approximately 0.9, 0.6 and 1.2 seconds on the measured machine. These are
profiled CPU measurements, not a claim about end-to-end API response time.

`tests/test_async_preparation.py` exercises complete input and results,
contextvars, exceptions, a real CPU predicate with a concurrent heartbeat,
authoritative cancellation and repeated task cancellation with worker cleanup.
