# Tool-intent classification of large requests

The classifier still normalizes and examines the complete request. Category,
reason, Unicode repair and first-match precedence remain unchanged. There is no
input limit, truncation, artificial prefix or classification based on length.

Three routing patterns have necessary-literal prefilters. A missing literal
proves the original pattern cannot match; a present literal always runs that
original pattern at its original position:

| Original reason | Necessary literal alternatives |
|---|---|
| bare weather lookup request | weather, forecast |
| rain lookup request | rain, precipitation, shower |
| current/latest lookup request | rate, price, news, weather, forecast, score, exchange, market, status |

Prefilters use the same Unicode `re.IGNORECASE` behavior as the original patterns.
They deliberately require no word boundary and impose no distance restriction.
For example, `rain` is necessary for both `raining` and `rainy`; `shower` is
necessary for `showers`. The original regex remains responsible for boundaries,
distance, syntax and classification. A lowercase substring test would not have
the same behavior for inputs such as `current prıce`.

The saved unprofiled CPU diagnostic for commit
`c2f75ce555bb573bdd9ffd5e6a4c33a49991bea2` measured the unchanged classifier at
8.491 seconds on the 3,900,000-character fixture, with a heartbeat maximum gap of
1.682 seconds. The three negative pattern searches measured 1.401, 0.916 and
1.156 seconds respectively. Those observations motivate this narrow change;
they do not establish an exclusive cause of service health timeouts.

Differential tests disable only the prefilters to exercise the complete original
classifier, without copying its implementation. They compare the full ToolIntent
on normal, Unicode, distance-boundary, mixed and large-tail inputs. A separate
test asserts that an absent necessary literal bypasses the expensive pattern.
One sequential before/after CPU pair used that exact 3,900,000-character fixture
and Python 3.13.7 without cProfile. Time changed from 7.058 to 3.888 seconds; the
maximum gap of a 5 ms heartbeat changed from 1.147 to 0.185 seconds. The full
ToolIntent was identical. An AST comparison also verified that the routing table
and other functions were unchanged and that only the two prefilter statements
were added to the classifier. This single pair is a local performance witness,
not statistical evidence or proof of API, MCP, Studio, health or Stop behavior.

The four pure routing test files passed 127 tests, including the new complete
result differentials. Inputs containing the necessary literals still run the
original patterns and may retain their original cost; the optimization does not
guarantee a latency bound for arbitrary text.
