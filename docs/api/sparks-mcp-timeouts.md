# Sparks deployment through MCP

`sparks_deploy` uses the existing Faustus API and controller. The native service
allows up to 60 seconds for a start request and 900 seconds for a stop request,
then refreshes endpoint metadata. The MCP HTTP bridge allows 120 and 960 seconds,
respectively, to observe the response. Status, sync and settings retain their
existing 40-second HTTP allowance. Malformed actions still reach normal API
validation with the existing allowance; the bridge does not invent an action.

The additional 60 seconds are observation margin, not a proven upper bound for
metadata synchronization. That path can wait for a shared lock, access storage and
inspect multiple endpoints. The bridge can still time out if those activities
exceed the margin; it does not report an unfinished operation as completed.

The deployment tool advertises `x-timeout-s: 975` in its MCP catalog, using the
existing timeout hint convention. A client that supports this hint can allow the
960-second HTTP observation plus 15 seconds of transport grace. An explicitly
shorter client deadline and cancellation remain authoritative. Clients that do not
read the hint keep their own timeout policy; publishing it does not change them.
Faustus declares MCP SDK `<2`; the native wire regression uses 1.29.1, matching the
installed runtime.

There is one HTTP attempt. An HTTP error remains an error, and caller cancellation
propagates without replaying a start or stop. Losing observation is not evidence
that the controller cancelled its operation. Use the existing status tool to
observe its state before deciding on another action. This change does not alter
the deployment lifecycle, select another model, restart a model automatically or
provide incremental deployment progress through this blocking tool. Prometheus's
normal deployment interface supplies that progress.

## Verification

Private real TCP responses delayed by 80 simulated seconds reproduce the old
40-second cut for both start and stop. A uniform HTTP clock scale of 1:100 keeps
the test short. The corrected bridge observes both responses and sends one POST.
The catalog and stop response also travel through the real MCP stdio server and
client; the fixture is an API bound to loopback, with no model or deployment.
Other regressions preserve the three unrelated allowances, HTTP 503, malformed
actions and cancellation after the API has received the request. The original
two MCP tests are included.

The corrected before-run has three failures and five passes; the final run has
13 passes. An earlier cancellation fixture relied on client startup finishing
within 200 ms; it was corrected to wait for observed request receipt before
cancelling. Its original result is retained separately. These transport tests
demonstrate observation and cancellation behavior, not the time or quality of a
real model start or stop.
