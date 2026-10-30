# Android emulator QA

The published library procedure is [android-emulator-qa](../../skills/library/android-emulator-qa/SKILL.md). Install it through the Skills library API or the product's Skills tab before relying on retrieval. It uses the existing Android MCP preset (mobile-mcp 1.0.6), not a new desktop-control bridge. A skill does not grant capabilities.

## Observed pilot and corrections

HoardDex 1836, installed build 375: Faustus made seven inputs and fresh textual observations through the visible Android Studio emulator, read Eevee → Vaporeon and its explicitly borrowed reference, and returned to Pokédex. It preserved game, save, language and the 1/1025 collection count. The initial tab was Collection and the requested return was Pokédex; this is not proof of identical tab/filter restoration. The final harness outcome was unverified because coordination/tool failures remained. The device journey succeeded; the full task did not pass cleanly.

The initial project had no active coordination template. The old controller's Faustus actor was still bound to its idle session. A controlled handoff paused the controller, preserved its histories, deactivated the old binding by revision and bound the pilot session to HoardDex. The final actor must be restored to that controller before it resumes. No model or app restart was needed.

The configured project root rejected the isolated HoardDex worktree as an out-of-root workspace. The UI-only pilot used the native project root without permission to change source files. The task also advertised qa/android outside the binding's allowed paths, so task claim failed even though its separate emulator lock succeeded. Correct the scope before a new clean pilot.

The normal runner registered the same named Android server twice across retries. Reuse the connected server rather than passing the registration specification again. The runner also omitted space_id when copying a template into a session binding. The corrected client copies that field and rejects a mismatching confirmation. It reuses only one equivalent, connected, enabled MCP with no extra environment or authentication; conflicting, ambiguous and pending registrations fail without changes. A setup failure retains its newly created session ID for reconciliation. Seven causal regressions failed before the patch; all 29 focused CLI tests pass afterward. The first review found that a later conflict could follow an earlier creation; the corrected runner preflights the entire list, including contradictory names within that list, before its first POST. Transport failures after creation starts remain potentially effective and must be reconciled.

Readback evidence: D:/LocalAI/research-inbox/faustus-android-coach/PILOT1836-AUDIT.json and PILOT1836-r3.ndjson. Runtime 285.48 seconds, 35 tool calls, 29 inference rounds, seven UI inputs. Reported aggregate input was 2,508,039 tokens despite a 96,000 runner budget: that option did not bound rounds inside this single native turn. Cached and processed counters must be read separately; do not present the configured number as an enforced consumption limit.

## Acceptance for the next run

Install and retrieve the procedure, use the existing server, claim the real task and device successfully, read actual initial state, execute the requested route with sequential action/readback batching where supported, and record an honest final-state comparison. Measure total rounds and time against the baseline; a shorter answer or fewer screenshots is insufficient. Android Studio stays visible; no storage reset, installation or settings change belongs to this navigation pilot.

## Second live round and retained learning

On APK377 the second turn successfully claimed1836 and completed it with a verified native harness outcome, zero failed tools, and unchanged Arceus/save/1/1025/226. It used29tools and22rounds rather than35/29, but308.84seconds rather than285.48: fewer rounds did not make this run faster. Eleven UI inputs included an avoidable keyboard detour. Version1.0.1 therefore makes submit/dismiss/readback explicit and avoids the unnecessary full skills catalogue lookup.

Global installation was effective: the native prompt audit selected android-emulator-qa1.0.0 atlevel1, fragment61e2fa7a5da007894c2133a06bde89732b1d6b694b6703cf7f3fe4bc380d64c2; the public skill API reportsuses1. The empty project-local list did not mean the global procedure was unavailable. No project copy or approval bypass is needed. Receipts: SKILL1835-PROMPT-RECEIPT.json and SKILL1835-AFTER-PHASE2.json.

The legacy ADB uiautomator dump command returned137 twice on this emulator while the app remained healthy and the existing Android MCP element driver worked. Preserve the error and use the connected driver; do not reboot or keep retrying the legacy dumper. Raw ADB package inspection and PNG capture still worked.

## Three-condition run and version1.0.2

Faustus read all three APK377 conditions and saved their PNGs, but its closing turn failed after38rounds/662.65seconds with context-window overflow. The task had already closed; this is not a clean completed inference turn. Root independently inspected all three images. Save attempts to the research directory and a project temp directory were rejected; relative paths succeeded in the MCP server working directory, D:/LocalAI/Faustus, and Root moved these own outputs to the evidence folder. No foreign files were moved.

Version1.0.2 requires compact text readbacks instead of automatic full JSON trees, a fresh chat for an independent bounded test, and correct screenshot path provenance. It retains histories and revision-checked actor handoffs. Root corrected the repeated regional label in HoardDex378; those377 screenshots deliberately preserve the earlier defect.

Faustus runs the local model on the configured Sparks and has no provider usage quota. Check resource availability and ownership, not usage limits. Quotas of Codex or other connected accounts are separate.

## Invalid final evidence and version1.0.3

The fresh1844 turn answered in35rounds/283.47seconds, but its final PNG showed Qwilfish's Collection detail instead of the evolution modal. Root opened the image and invalidated the claim, reopened thread1997 and preserved QA1844-INVALIDATED.txt. An answered turn, a saved filename and quoted condition text did not establish the final screen. Artifact retrieval also had an outcome_unknown response; it must not be relabelled success. This run does not prove satisfactory autonomous visual verification.

Version1.0.3 requires a fresh final listing with the intended screen title, requested condition and source note simultaneously, plus inspection of the corresponding pixels or an explicit text-only handoff. It prohibits borrowing the expected state from the brief or an earlier observation. Root subsequently checked the real APK378 modal through ADB and restored ES/system/Arceus/Pokedex,1/1025,226. The OS font scale stayed1.0; an automatic approval rejection prevented the proposed200% check, so that native claim remains unverified. The procedure change still requires a new bounded live test; it is not itself proof that the model now follows it.

## Failed1845 regression and advertised-tool guard

The1.0.3 regression on APK379 failed after48rounds,51tools and250.94seconds with context overflow. It exceeded its12-input brief, selected Perla Reluciente and stayed in the game picker with Z-A expanded. Its final element listing correctly exposed the base tab as a labelled View; Root selected it by ADB, checked the379 modal and restored ES/system/Arceus/Pokedex,1/1025/226. This is Root recovery, not successful autonomous navigation. No final requested PNG was produced by1845.

The model invented mobile_tap/mobile_scroll, absent from the live33-tool catalogue. McpManager dispatched those names and treated McpError as a possibly applied write. The new preflight refuses a name missing from a known complete catalogue before transport, with MCP_TOOL_NOT_ADVERTISED and effect_not_dispatched. Empty legacy placeholders remain distinct from an explicit completed zero-tool discovery. Real dispatched writes with lost replies retain outcome_unknown and are never silently replayed. Two causal regressions failed before the fix. Independent review rejected its first version because it also rejected valid tools on undiscovered pages; two further regressions fail there. The correction respects discovery_truncated, including an empty first page, and46 focal tests pass with async support. The earlier12-pass/32-skip run was incomplete; the initial broad run was interrupted after exposing the legacy-placeholder incompatibility, which is also corrected. These receipts preserve the rejected versions.

Version1.0.4 teaches labelled View tabs, advertised names and scaling resized image coordinates to native device pixels. Source tests and parsing do not certify a successful new mobile journey; review, integration and a bounded live rerun are still required.

## Bounded379 readback and final controller restoration

The advertised-tool guard and its partial-catalogue correction were independently approved at submission2 and integrated as284f94ea/df1ba611. The backend was restarted through its managed runtime, preserving the existing33-tool Android registration, emulator and model servers. Real writes with lost replies remain uncertain and are not replayed.

The normal library installation retained another owner's canonical android-emulator-qa1.0.3 and published1.0.4 as android-emulator-qa-2. A normal update of the canonical entry returned404; ownership was not bypassed. Availability of the new entry does not prove its prompt injection or causal effectiveness.

For1848 Root prepared the open game picker with the Z-A base tab fully visible. Faustus selected its fresh View reference and the information button, then saved the original720x1600 PNG. Root opened QA379-BOUNDED104.png and confirmed the actual evolution modal, unique regional Qwilfish title,20-target condition and normalized-source note together. These two actions succeeded from the prepared state. The closing turn then searched files, including a needless whole-drive search, rather than finishing the short handoff. Root cancelled that exact run through the normal run-ID-checked API and restored ES/system/Arceus/Pokedex,1/1025/226. This is not a clean autonomous journey or evidence of improved overall efficiency.

Relative screenshots belong to the MCP server's declared working directory, not the native project's workspace. Here that directory is D:/LocalAI/Faustus. Report the declared save result and leave pixel inspection to Root when unavailable; do not search drives to manufacture an additional confirmation. The preserved failure is RESULT1848.txt and the restored screen is ROOT1848-RESTORED.png in the same evidence directory as earlier pilots.

The original periodic controller's binding was restored with its original scope at revision15 and resumed through its normal launcher. Experimental bindings were retired, histories and outcomes retained, and model/device leases released. This does not fix the separate controller/context issue1802.

## Whole-run SDK deadline

Task1849 used a separate local coding helper identity, faustus-sdk, so the periodic controller did not need another handoff. Faustus produced a draft and six regressions, but its turn failed after43tools/31rounds/995.82seconds with context overflow despite a600-second CLI budget. Root preserved that draft, corrected the independently reproduced gaps and integrated the three reviewed commits through1b26bfe2.

The client now applies a monotonic absolute deadline to setup, response opening and streaming, including silence and partial-line trickles. Expiry attempts only an exact observed-run stop, with a separate absolute two-second cleanup allowance. Missing identity or acknowledgement is reported as uncertain; an already-sent setup request is not retried. Resuming a new leg never reuses the prior leg's ID. Main passes44CLI tests; independent review also passes four adversarial/control scenarios. These HTTP tests use synthetic loopback services, not a new live-model or Android success. The helper binding/template were retired by revision after its terminal state, without changing the general controller.
