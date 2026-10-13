# Native model councils

Council keeps its native rooms, roles, transcript, task ledger, decisions, observer stream and Dispatch execution path. It does not create another agent loop. Group chat remains available for freely assigned tool-using participants; Council offers its existing discussion, blind comparison, debate, collaboration and driver/reviewer workflows.

New rooms use the configured global model, currently Sparks. The interface initially offers two seats on that service, rather than the first PC models in the catalogue. Explicit model/endpoint choices remain available. A configured unavailable global service stays selected.

Default round, turn, wall-time, token and room-concurrency budgets are0: unlimited. Positive values preserve explicitly chosen limits. There are no fixed100round/1000turn/32parallel execution ceilings or eight-seat UI ceiling. Physical shared model/GPU reservations still apply. Provider transport timeouts remain native; Council does not add a120s intervention deadline or30s reservation deadline. Dispatch tasks inherit an explicit positive room wall limit, or wait without another imposed1800s deadline. Stop, pause and cancellation remain available.

A positive one-round budget permits that first round and prevents the next; it does not consume the round before its speakers can start. Native replacement text retracts the earlier text. Named native error frames preserve a provider failure instead of accepting a partial answer as success.

The existing feature switch stays available, enabled by default for new configuration. An explicitly saved off value remains off until changed. Existing rooms, budgets, roles, observations and evidence are preserved.

The built-in `council` MCP server uses the existing `/api/council` API. It exposes options, list, create, get, update, send, command, messages, usage, ledger, tasks and decisions. Configuration lists the existing commands. Sends use a stable `idempotency_key`; updates use the actual `expected_revision`. Domain refusals retain their native error code. MCP departure does not cancel the native service's turn.

`FAUSTUS_INTERNAL_API_URL` targets the own Faustus instance and `FAUSTUS_INTERNAL_TOOL_TOKEN` uses its existing internal identity. No-login mode uses the existing local storage owner. An authenticated internal MCP client may set `FAUSTUS_COUNCIL_OWNER` explicitly. Cookie ownership and native admin/role policies remain authoritative.

Verification uses private product data and scripted HTTP inference, without GPU/model changes:25 sequential native turns and50 model contributions continue after MCP departure and exceed former4round/20turn defaults. A requested positive budget1 permits the first two speakers and makes0calls in the following blocked round. Unit regressions also advance beyond old wall/token caps and round-trip300seat identifiers. These checks establish the covered contracts, not a universal ranking or a claim that arbitrary hardware has unlimited capacity.
