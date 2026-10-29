# Android emulator QA

The published library procedure is [android-emulator-qa](../../skills/library/android-emulator-qa/SKILL.md). Install it through the Skills library API or the product's Skills tab before relying on retrieval. It uses the existing Android MCP preset (mobile-mcp 1.0.6), not a new desktop-control bridge. A skill does not grant capabilities.

## Observed pilot and corrections

HoardDex 1836, installed build 375: Faustus made seven inputs and fresh textual observations through the visible Android Studio emulator, read Eevee → Vaporeon and its explicitly borrowed reference, and returned to Pokédex. It preserved game, save, language and the 1/1025 collection count. The initial tab was Collection and the requested return was Pokédex; this is not proof of identical tab/filter restoration. The final harness outcome was unverified because coordination/tool failures remained. The device journey succeeded; the full task did not pass cleanly.

The initial project had no active coordination template. The old controller's Faustus actor was still bound to its idle session. A controlled handoff paused the controller, preserved its histories, deactivated the old binding by revision and bound the pilot session to HoardDex. The final actor must be restored to that controller before it resumes. No model or app restart was needed.

The configured project root rejected the isolated HoardDex worktree as an out-of-root workspace. The UI-only pilot used the native project root without permission to change source files. The task also advertised qa/android outside the binding's allowed paths, so task claim failed even though its separate emulator lock succeeded. Correct the scope before a new clean pilot.

The normal runner registered the same named Android server twice across retries. Reuse the connected server rather than passing the registration specification again. The runner also omitted space_id when copying a template into a session binding. The corrected client copies that field and rejects a mismatching confirmation. It reuses only one equivalent, connected, enabled MCP with no extra environment or authentication; conflicting, ambiguous and pending registrations fail without changes. A setup failure retains its newly created session ID for reconciliation. Seven causal regressions failed before the patch; all 27 focused CLI tests pass afterward.

Readback evidence: D:/LocalAI/research-inbox/faustus-android-coach/PILOT1836-AUDIT.json and PILOT1836-r3.ndjson. Runtime 285.48 seconds, 35 tool calls, 29 inference rounds, seven UI inputs. Reported aggregate input was 2,508,039 tokens despite a 96,000 runner budget: that option did not bound rounds inside this single native turn. Cached and processed counters must be read separately; do not present the configured number as an enforced consumption limit.

## Acceptance for the next run

Install and retrieve the procedure, use the existing server, claim the real task and device successfully, read actual initial state, execute the requested route with sequential action/readback batching where supported, and record an honest final-state comparison. Measure total rounds and time against the baseline; a shorter answer or fewer screenshots is insufficient. Android Studio stays visible; no storage reset, installation or settings change belongs to this navigation pilot.
