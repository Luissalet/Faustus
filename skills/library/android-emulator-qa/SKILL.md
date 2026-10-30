---
name: android-emulator-qa
description: Exercise a real Android application through the visible emulator using fresh element readbacks, bounded actions and a measured return state. Use when testing Android, an emulador Android, ADB or a mobile user journey while preserving the user's data.
version: 1.0.1
category: testing
tags: [android, emulator, adb, mobile, qa, restoration]
status: published
source: imported
---

## When to Use

Use for a short, observable Android journey on an already available emulator or connected device. The procedure works with the native Android MCP preset or explicitly authorized ADB. Installing this skill supplies a procedure; it neither installs a bridge nor gives permission to reset a device. Textual accessibility evidence proves labels and navigation, not visual polish.

## Procedure

1. Establish one owner and one small outcome. Read the specific task and claim its device resource through the configured coordination tools before input. The project, actor, space, task paths and permitted resource must agree. A task claim rejected by scope is a configuration failure: report it, and have the owner correct the binding. A successful separate device lock does not make the rejected task claim successful. Do not substitute another actor or invent coordination parameters. Avoid expanding a bounded task into a global inbox tour.

2. Check the actual capabilities before planning inputs. Reuse an existing connected Android MCP server; repeated registration can create duplicate servers. Discover the matching Android category once, then load only device listing, foreground app, element listing, click, typing and Back as needed. Do not choose desktop mouse tools for an Android task. An installed global skill can already be injected into the turn even when project-local skills_list is empty. Use the included procedure; do not traverse the entire skills catalogue to prove its use. Keep the emulator visible where requested, including an emulator embedded in Android Studio. Do not launch another instance or restart the current one to make discovery easier.

3. Resolve identity from the tool's device list. An MCP device identifier can be an AVD name while its ADB serial is emulator-5554; these are different interfaces, not interchangeable strings. Confirm the mapping and foreground package before acting. With ADB, use the verified absolute SDK executable and an explicit serial on every command. If the executable is absent from PATH, locate the SDK once instead of repeating a failing bare command. Verify boot completion and package version without installing or restarting.

4. Record a baseline from the screen actually observed. Include foreground package, active tab, game, save, language, search/filter state and visible collection counters. An expected baseline in a brief is an expectation, not an observation. Different tabs can display different result counts. State which return destination the brief requires and which values must remain unchanged; do not claim a previous tab's filter was restored if it was never read. Save compact evidence rather than repeating the entire device tree in the final answer.

5. Prefer current element references over coordinates. After navigation or any layout change, get a new element listing before choosing the next control. A reference is scoped to that observation. A click followed by an element listing can be a sequential batch when the batch schema supports it; choose the next target only after inspecting that readback. Do not queue multiple navigations using references from one old tree. Record the action and the visible result. Typing must be followed by reading the field or its filtered results. For search, use the submit option when supported, then confirm that the keyboard no longer covers the intended result. A successful tap sent to a covered row can type into the keyboard instead of opening that row.

6. Keep a small action budget, normally twelve inputs for a smoke journey. If an action leaves the state unchanged, inspect focus, keyboard, a modal or scroll position before trying again. Two unsuccessful attempts at the same outcome end blind retries. Use an observed text field, a small scroll in its actual container or documented Back when appropriate. If the route remains blocked, report the last confirmed state and stop inputs. Do not reset storage or change unrelated preferences as a shortcut.

7. Read the actual result, including uncertainty and source labels. In a game guide, a method labelled as a reference from another edition is not a confirmed local method. Record the selected edition and the displayed qualifier with the condition. Opening a screenshot does not imply the model has inspected its pixels; explicitly separate a textual readback, a saved image and a visual inspection.

8. Return through observed controls and compare with the baseline. Close the modal and keyboard, follow the required destination, and read the resulting tab and preserved values. A saved item count remaining equal does not prove every filter stayed equal. If the brief intentionally ends at a different tab, describe that difference. Never manufacture the expected state by silently changing captures, favourites, saves or settings.

9. Release ownership after the final readback and publish one concise receipt in the task's thread: installed build, route, condition actually read, input count, final state and any failures. Complete the task only when its acceptance conditions are met. A terminal model answer and an unverified harness outcome are separate facts. Preserve the failure receipt for the next improvement; avoid an extra terminal audit that merely repeats already sufficient mobile evidence.

## Pitfalls

A connected server is not proof that the project exposes it. A project workspace must lie inside its configured root; a UI-only task can use that root without editing repository files, whereas code changes require a properly configured isolated worktree. Keep registration, binding and workspace failures separate from emulator failures. Do not reconnect a healthy device to fix a coordinator error. Do not claim physical ARM64 testing from an x86 emulator, successful tapping as visual verification, or build availability as installation proof.

## Verification

A useful receipt has before/after observations, a mapped device identity, a confirmed package/build, current references for every input, and the requested visible result. A second independent readback must confirm the return state. Compare action count, failed tools, inference rounds and elapsed time with the prior run when improving efficiency; fewer UI clicks alone do not prove the whole workflow got faster. Leave unresolved failures explicitly open. Reference scenario: HoardDex task 1836 navigated Guide, Evolutions and Eevee in seven inputs, and exposed a task-scope mismatch plus avoidable discovery and completion-audit overhead.
