---
name: android-emulator-qa
description: Exercise a real Android application through the visible emulator using fresh element readbacks, bounded actions and a measured return state. Use when testing Android, an emulador Android, ADB or a mobile user journey while preserving the user's data.
version: 1.0.18
category: testing
tags: [android, emulator, adb, mobile, qa, restoration]
status: published
source: imported
---

## When to Use

Run short journeys through native Android MCP or authorized ADB. Use accessibility text for labels and navigation, and inspect pixels for visual polish.

## Procedure

1. Agree on one outcome and owner. Claim the device for the exact task before input. Project, actor, space and resources must match. Report a rejected binding to its owner even if a separate device lock succeeds. Use the exact identity and parameters. Keep work scoped to the task. Start an independent native chat after the prior turn ends, preserving history and using the revision-checked actor binding.

2. Discover Android MCP tools once and load only those needed. Use the injected global skill even when project-local skills_list is empty. Keep the running emulator visible if requested, including inside Android Studio.

   Check the execution host. A model on a Spark can plan or review while Windows runs ADB. Resolve the serial on the emulator host or its connected Android tool. If neither is reachable, send acceptance checks to the device owner through the task thread and request the receipt in step 9.

3. Match the MCP device ID to its ADB serial. Use the absolute SDK executable and explicit serial. Verify boot and package version.

   For an authorized update, allow a bounded transfer deadline suited to the device and APK. If installation stalls, check the original ADB process and matching package session before retrying. Record elapsed time and phase. Once installed, check version. For exact identity, resolve the APK with `pm path` and compare SHA-256. After a cold start, `am start -W` confirms activity launch. Wait within a separate deadline for the actual task content. At expiry capture the visible screen and scoped logs before further input.

   Before coordinate input and after installation, app switches or dialogs, confirm both foreground observations. Run `python <skill-directory>/scripts/adb_foreground.py --adb <absolute-sdk-adb> --serial <serial> --package <expected-package>`. Exit 0 matches the package, including launcher aliases. Use `--wait-ime-ms 1500` with an expected IME state after an observed field tap or keyboard dismissal when Android is still animating. The guard reads both foreground observations again during the short settle window and stops if the app loses focus. It sends no input. An exit 2 identifies the activity or window probe that failed.

4. Record the baseline tab, game, save, language, filters and counters. Use observed values to verify restoration.

5. Use advertised tools and fresh references; labelled View nodes can be actionable tabs. After navigation or any layout change, get a new element listing before choosing the next control. A reference is scoped to that observation. A click followed by an element listing can be a sequential batch when the batch schema supports it; choose the next target only after inspecting that readback. Do not queue multiple navigations using references from one old tree. Record the action and the visible result. A dispatch acknowledgement may precede rendering: use a bounded fresh observation for a pending transition before repeating a mutation. For mobile_batch_commands, use listElementsAtEnd=false and an explicit mobile_list_elements_on_screen with format=text; automatic full JSON trees can exhaust the model context. Read the exact query and results after typing and after IME submit, which can autocorrect names. If only hiding the visible keyboard, use Back. Refresh before tapping uncovered results. Add `--expect-ime hidden` to the foreground guard before tapping where the keyboard covered a result.

   Use the same helper with `--list` in place of `--label` to discover up to 80 enabled, labeled nodes inside the observed rectangle. It reports the total match count and retains multiple nodes with the same label. After navigation, use `--wait-tree-ms 6000` to retry a failed hierarchy read without sending input. Select a target only after checking its bounds and context.

   To resolve a duplicate label or read back a field, run `python <skill-directory>/scripts/adb_ui_query.py --adb <absolute-sdk-adb> --serial <serial> --package <expected-package> --label "<observed label>" --class android.widget.EditText --rect <left> <top> <right> <bottom> --min-height <observed field height> --expect-ime hidden`. The rectangle excludes the visible app header, footer and overlays. The helper reads a fresh tree and returns the actual text and center only for a unique matching node. It sends no input. After a confirmed navigation, add `--wait-tree-ms 6000` if the field has yet to settle. The helper retries a missing, incomplete or ambiguous fresh tree only while the expected app and IME still match. A transport timeout or package change ends the observation. When a match is clipped, scroll the field clear and query again. Compare the actual text after typing or closing the IME before committing a form.

   A control can have plausible screen coordinates while hidden under a fixed app header or bottom navigation. Use the observed unobscured content area, not just the physical screen bounds. Scroll the whole target into that area and get fresh references before tapping. Distinguish the app header from system bars, cutouts and the actual IME. Dismiss a covering IME through observed permitted controls, then refresh. A header control persistently behind a system bar or cutout is an app defect: record it rather than replay taps or scroll the body; use observed exterior close or Back if permitted. After a picker tap, confirm that its filter or options actually appeared before typing. Read translated labels from the current tree rather than guessing their wording.

6. Keep a small action budget, normally twelve inputs for a smoke journey. If an action leaves the state unchanged, inspect focus, keyboard, a modal or scroll position before trying again. Two unsuccessful attempts at the same outcome end blind retries. Use an observed text field, a small scroll in its actual container or documented Back when appropriate. If the route remains blocked, report the last confirmed state and stop inputs. Do not reset storage or change unrelated preferences as a shortcut.

   A reachable ADB transport and `sys.boot_completed=1` can coexist with unresponsive activity and window services. Record which service failed before any input. Verify the AVD name with `adb -s <serial> emu avd name`. If a restart is already in scope and the guest cannot reboot, relaunch the same visible AVD with its existing userdata and a cold start. Wait for both service probes to recover, confirm the installed build again and resume from a fresh listing.

   If the guest shell times out or UI Automator exits while the emulator console still responds, try the SDK console's display capture. Check `adb -s <serial> emu help screenrecord screenshot`, then use `adb -s <serial> emu screenrecord screenshot <existing-host-directory>` in a dedicated evidence folder. Identify and inspect the new PNG. It can have physical display dimensions and a device frame even when `adb shell wm size` reports a logical override. Use the actual dimensions and coordinate space of the input method. An `OK` reply or a frame that still shows the prior screen is not proof that a tap landed. This fallback applies to tool or guest failures within the authorized QA session.

   Focus is not proof that the keyboard is visible. On Android, mIsInputViewShown can remain true while the actual IME InsetsSource has visible=false. Use `--expect-ime visible` to confirm a single observed IME state before Back; Back with an already hidden keyboard can leave the current tool. If navigation changes, reconcile the new screen before resuming.

7. Read the actual result, including uncertainty and source labels. In a game guide, a method labelled as a reference from another edition is not a confirmed local method. Record the selected edition and the displayed qualifier with the condition. For resized images, convert image coordinates to device pixels using the reported dimensions. Distinguish textual readback, saved images and inspected pixels. The mobile server permits its working directory and host temporary directory; a project temp folder may not qualify. Relative output paths belong to that server, not necessarily the chat workspace. Verify the real file before citing it.

8. Before declaring success, get a fresh compact listing after the last navigation. Repeat `--require-label` with `--list` to require the intended screen/modal title, requested condition and source note in one hierarchy when all fit in the observed rectangle. Use `--wait-tree-ms` during a transition. The result includes the matched nodes and any missing labels. Capture this state and inspect its actual pixels; file creation alone proves no content. If pixels are unavailable to the model, mark text-only and checkpoint for visual review. Return via observed controls, close the modal or keyboard, and confirm the destination, game, save, filters and counters from a fresh read. State any intended difference from baseline. Never edit saved data or settings to manufacture restoration.

9. After the final readback, release ownership and post one receipt with the execution host, serial, installed version and SHA if verified, route, observed condition, inputs, restored baseline and failures. Finish when the receipt meets the acceptance checks. Keep a failure receipt for follow-up. A model answer without device observations is a plan.

## Pitfalls

A connected server is not proof that the project exposes it. A project workspace must lie inside its configured root; a UI-only task can use that root without editing repository files, whereas code changes require a properly configured isolated worktree. Keep registration, binding and workspace failures separate from emulator failures. Do not reconnect a healthy device to fix a coordinator error. Do not claim physical ARM64 testing from an x86 emulator, successful tapping as visual verification, or build availability as installation proof.

## Generated Images and Sharing

Distinguish app preview, generated image, Android share sheet and recipient delivery. An export button or code proves no PNG. Inspect the attached-image thumbnail for the full composition, including groups outside the preview's scroll viewport; it does not certify original dimensions, bytes or every label.

Use a named disposable QA fixture. A blank template omits selections; answers preserve their marks. Cancel the share sheet for a prepare-without-sending test, confirm the busy state clears, remove only the fixture and verify the return state. Selecting a recipient is a separate action.

If retrieval is blocked, preserve its reason and report the unverified layer. Do not evade the restriction through another surface, protocol or capture method. Android generation does not certify a web download.

## Verification

A useful receipt has before/after observations, a mapped device identity, a confirmed package/build, current references for every input, and the requested visible result. A second independent readback must confirm the return state. Compare action count, failed tools, inference rounds and elapsed time with the prior run when improving efficiency; fewer UI clicks alone do not prove the whole workflow got faster. Leave unresolved failures explicitly open.
