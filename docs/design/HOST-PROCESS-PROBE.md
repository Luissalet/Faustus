# Desktop-app process discovery

On Windows, desktop tools still require the running desktop app so that its
indicator and Escape stop are available. A fresh heartbeat still takes
precedence, the process fallback still recognises only Electron/Faustus
processes whose executable or command line belongs to this installation,
and operator overrides retain their existing behavior.

The fallback now enumerates process names first and reads executable paths
and command lines only for matching candidate names. Previously process_iter
requested all three attributes before filtering: every unrelated process paid
for Windows command-line inspection. A live nonblocking stack sample found
this work on the server event-loop thread while preparing tool schemas.

The five-second result cache starts when discovery completes. A slow lookup
therefore does not consume its own cache lifetime before callers can use it.
The TTL, heartbeat rules, application identification and offered permissions
are unchanged. Exited or inaccessible candidates remain best effort.

A frozen external oracle imports the actual source alone. Its first case uses
real Windows/psutil enumeration and counts detailed reads without emitting
process names or command lines; remaining cases use explicit process/cache
seams. Before the change:3PASS/4FAIL,1367 unrelated detailed reads and37.860s
for that observed scan. After:7PASS/0FAIL,zero unrelated detailed reads and
11.968s for the initial observed scan. These timings are individual source
observations, not a controlled end-to-end agent speed comparison. Initial
process enumeration still has a cost. A repeated-probe benchmark and native
service trial are recorded separately after integration.

The15 selected tests pass in an isolated Windows venv with --noconftest:
eight new regressions and seven existing desktop availability tests. The
three warnings are pytest settings for absent asyncio/timeout plugins; these
selected synchronous tests do not use those plugins. No app, model, screen
control or live data is exercised by that pytest run.
