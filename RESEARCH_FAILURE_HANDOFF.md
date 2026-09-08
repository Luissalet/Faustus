# Deep Research failure — handoff to Claude

Workspace: `D:\LocalAI\odysseus`. Diagnose/fix the existing implementation; preserve the uncommitted UI and desktop-control work. No fix for this failure has been applied in this handoff.

## Confirmed failure

Log: `logs/faustus-desktop-20260908-194110.log`, lines 338–474. Session `rp-a7f62de3b905`, 2026-09-08 19:45:34–19:45:50, model `qwen3.8:27b-q8_0`, Ollama `127.0.0.1:11434`.

- Research failed BEFORE searching, in `_probe_endpoint`, not while processing the long research prompt.
- `/api/tags`, `/api/ps` and `/api/show` returned HTTP 200.
- The generation probe was translated from `/v1/chat/completions` to native `/api/chat`, `think=False`.
- At 19:45:50 the log says `LLM async read timed out after 15.13s`, then `504: POST http://127.0.0.1:11434/api/chat timed out after 1 attempts`.
- `src/research_handler.py:740` calls `llm_call_async` with prompt `hi`, max_tokens=5, timeout=15, max_retries=1. This aborts the whole research when the model does not finish that probe within 15 seconds.
- A cold load/reload or slow local inference is a plausible explanation, NOT proven by this log. The log reports runtime context 32768; do not claim this failure proves it was running at 262144. Do not conflate the 300s research time or 1800s hard timeout with this 15s probe.
- Earlier ChromaDB errors on port 8100 are startup errors; the actual failure traceback is the Ollama probe timeout.

## Proposed correction

1. Make local model readiness tolerant of model loading. Separate connection timeout from read/generation timeout; configurable bounded local warm-up timeout (e.g. 120–180 seconds), respecting cancellation and overall run timeout. Use existing endpoint/provider classification, not just a port-number heuristic. Keep auth, missing-model and other concrete upstream failures actionable. Show “Loading/checking model…” while waiting. Avoid rapid retries that queue duplicate generations; verify the model's readiness rather than declaring the whole endpoint unreachable. Preserve the user's selected model and context.
2. Fix error propagation end to end. `routes/research/research_routes.py` SSE loop currently can send a progress event with terminal `status='error'` BEFORE the separate final event containing `error`. `studio/src/adapters/research.ts::followResearch` closes EventSource on the first non-running status, so it can discard the real error. Emit one terminal event with the error, or include it in every terminal payload. Ensure `/status` and polling expose the same error. `src/research_handler.py` stores exception text in `entry['result']`, but its error branch does not populate progress.message; `Research.tsx` falls back to “The research failed.” when no message survives.
3. Prevent `Research.tsx` recovery from treating any nonempty `researchResult().result` as success: `entry['result']` may be an error string. Carry/check the result status explicitly before switching outcome to done.
4. Add regression tests: local probe completing after 15s with mocked timing; genuine connection/auth/model failure; cancellation during warm-up; first SSE observation already terminal; SSE failure falling back to polling; upstream error is visible, not generic, and never displayed as a successful report. Existing starting points: `tests/test_research_probe_errors.py`, `tests/test_research_status_avg_duration.py`, research route tests.
5. Rebuild frontend after changes and verify with a real cold-loaded local model. Do not launch the user's long medical research without intending to run it; a small diagnostic prompt is sufficient for readiness verification.

## Existing work to retain

Deep Research now uses the same model routes as chat, preserves resolved model in progress, has a large prompt editor and the chat Vitals pill/menu. Local model Options show VRAM/RAM/spill estimates and maximum estimated context. There are substantial other uncommitted changes in this checkout; do not reset them.
