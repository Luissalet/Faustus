---
name: game-release-checklist
description: Produce a release checklist and go/no-go for a game build — codebase health, build verification, quality gates, content complete, platform requirements for PC, console or mobile, store certification for itch.io or Steam, store page and launch readiness — scanning the project for evidence and marking unverifiable items as not assessed. Use when about to ship a demo, early access, store release or patch of a Unity or other game.
version: 1.0.0
category: testing
tags: [gamedev, unity, release, launch, checklist, steam, itch, build, qa, go-no-go, lanzamiento, publicacion]
status: published
source: imported
---

## When to Use

A build is about to go to players: a jam or demo upload, an itch.io page, a Steam (or console) release, an early-access update or a patch. Only emit items for the platforms and store tier actually targeted.

Adapted from Donchitos' game studio skill pack (MIT, pinned 05ad7c8a5b8a, release-checklist skill). See THIRD_PARTY_NOTICES.md.

## Procedure

1. Ask once (`ask_user`) for version, target platforms and stores, and release type (demo, early access, full, patch). Skip items for phases the project has not reached.
2. Scan the project for evidence: `git_status`/`git_log` for a clean tagged commit, `grep` for TODO/FIXME/HACK in scripts and content, placeholder asset names, hard-coded player-facing strings, debug flags left on, open bugs on the board (`board_list`).
3. In Unity, run the real tests with the `unity` tool (EditMode and PlayMode) and read compiler and console errors; produce the build in batch mode when the Editor is closed and record its size and version string.
4. Codebase health: no compiler errors or new warnings, TODO/FIXME resolved or documented, debug code disabled.
5. Build verification: clean build on every target, all assets load, size within budget, version set, reproducible from the tagged commit.
6. Quality gates: no open critical bugs; high and medium bugs listed as accepted risks; critical-path features tested; performance within the frame-time and memory budgets on the minimum spec; no regression against the previous build; a long soak session without crash or leak.
7. Content complete: placeholders replaced, text proofread and localisation-ready, audio mix final, credits and third-party licence attributions complete.
8. Platform: PC — minimum and recommended specs, keyboard and mouse, controllers, resolutions and ultrawide, windowed/borderless/fullscreen, settings persist. Mobile — store guidelines, permissions justified, privacy policy and data-safety labels, touch on several screen sizes, battery, background/resume. Console — the platform's certification list, suspend/resume, user switching, storage full.
9. Store: itch.io — upload size, page complete, honest tags, browser vs download build tested, channel names. Steam — SDK integrated, depot installs from a fresh account, achievements and cloud saves work, Steam Deck if targeted, content rules reviewed. Store page metadata, screenshots, trailer, capsule art, age rating, pricing, EULA and privacy policy.
10. Launch readiness: crash reporting reachable, day-one patch plan, who watches the first 72 hours.
11. Verdict: READY, NOT READY (list blockers with owners) or NOT ASSESSED (list what could not be verified). File blockers with `board_create`.

## Pitfalls

- Ticking items without evidence; an unchecked item is "not assessed", never assumed done.
- Emitting console or certification items for a PC-only itch.io demo.
- Building from a dirty working tree, so the shipped build cannot be reproduced.
- Measuring performance only on the development machine.
- Forgetting licence attributions for plugins, fonts, music and assets.

## Verification

- Each checked item cites its evidence (test run, build log, file, screenshot, store page).
- Tests were run in this session and their counts are in the report.
- The build was installed and launched from the produced artifact, not from the Editor.
- The verdict follows from the items: any open blocker means NOT READY.
