# Skill library

## Recorded Unity verification

Six original procedures extend the existing `gamedev` and game-production
workflows: `unity-animation`, `unity-collision`, `unity-speed-feel`,
`unity-hdrp-lighting`, `unity-model-import` and `unity-recorded-playtest`.
They use the installed editor bridge and Blood Arrows' recorded journeys,
with actual input, collision callbacks and live Animator/bone samples.
They require reading the generated report and watching the recordings;
compilation and a configured clip do not establish gameplay behavior.
The new procedures do not install Unity or change the game by themselves.

This folder ships a curated set of `SKILL.md` procedures with the app
itself — engineering habits (TDD, verification, security review, per-
language patterns, research, planning) that would otherwise take a while
for a local model to rediscover on its own.

## Local production and evidence workflows

The library also includes procedures for reverse engineering, editable
Blender assets, deterministic motion composition, revision-aware media,
code impact analysis, entity investigation, historical evidence and local
voice comparison. Each has a small worked scenario in `references/` and
records its methodological source and revision.

`gamedev` reads the actual project engine and version before selecting Unity,
Godot, Unreal or browser-3D references. Structured live editor operations are
currently implemented for Unity. Other references guide the existing file,
terminal and verification tools; they do not claim an installed engine bridge.

Installation preserves these supporting files beside the installed skill.
They are read on demand through the existing skill reference tools, rather
than added to every prompt. The procedures need no paid service or hosted
API. They describe how to use available tools; installing a procedure does
not install a renderer, a speech model or a new application capability.

`diagram-authoring` uses Vitruvius's revisioned graph editor and native
object operations. `source-explainer-production` uses Prospero's supplied
sources, scenes, local voice and resumable MP4/SRT production; for informative
videos it keeps a FACTS.md and checks that numbers shown are in it. They check
the installed tool catalogue before using a capability. `deterministic-motion-production` adds a direction flow for films that must look
like a known style (style card, treatment, cues, sync and reading checks) that
uses Prospero's Montage tools when the installed build has them. The existing
design and deterministic-motion procedures route to the fused owners;
they do not launch the absorbed applications. `image-composition` uses
Prospero's retained pixel boxes, exact text, reference images, schematic
preview and existing image-generation route. It distinguishes installed
capability, compilation, demo transport and actual image-model adherence.

## Web product delivery

legacy-web-app-onboarding receives an inherited prototype: static audit,
reproducible start, a feature inventory that becomes the no-regression
contract, baselines and a short question list. web-3d-asset-pipeline
turns meshes into checked metrics, a layer-based fabrication estimate for
quoting and compressed GLB levels of detail, with viewer and toolpath rules;
its scenario holds a hand-checked cylinder. product-ui-visual-direction
decides where a product UI may be striking and keeps work screens quiet,
with motion rules and a bounded verification pass through Vitruvius and the
design detector.

## Web motion, media, page design and game production

Added 2026-11-03, each adapted from a pinned upstream commit (sources,
SHAs and licences in `THIRD_PARTY_NOTICES.md`):

- **GSAP** — `gsap-core`, `gsap-timeline`, `gsap-scrolltrigger`,
  `gsap-plugins`, `gsap-utils`, `gsap-react`, `gsap-performance`,
  `gsap-frameworks`: tweens, sequencing, scroll-linked animation, plugins
  (Flip, SplitText, DrawSVG, MorphSVG, Draggable), helpers, React and
  Vue/Svelte lifecycles, and frame-rate work.
- `ffmpeg-video-editing` — probe first, lossless when possible, a fixed
  step order, measured checks and a contact sheet after picture changes,
  using `inspect_media`, `transform_media` and FFmpeg through the shell.
- `hallmark-page-design` — a floor of rules for new pages that should not
  look generated, one question round, the hero first, then a sweep.
- `unslop-interface` — a nine-pass redline of an existing generic-looking
  screen from a five-line direction (CC BY 4.0, credited).
- `browser-motion-graphics` — promos, openers, kinetic type and explainers
  as one self-contained HTML/GSAP file driven by timeline time, with
  checks against slideshow structure.
- Game production for Unity and other engines — `game-design-document`,
  `game-sprint-stories`, `game-playtest-report`, `game-balance-check`,
  `game-release-checklist`; they file work with the project board tools
  and run tests through the `unity` tool.

Every one is tagged (`gsap`, `animation`, `ffmpeg`, `video`, `design`,
`gamedev`, `unity`, plus Spanish keywords) so the per-turn skill selector
matches them once installed.

## Vault notes, canvases and fresh-context review

Added 2026-11-10 (sources and licences in `THIRD_PARTY_NOTICES.md`):

- `obsidian-markdown` — the vault dialect: wikilinks with headings, blocks
  and aliases, embeds, callouts, properties, nested tags, block ids,
  comments; with the Cerebro's own rules (mirrored notes keep `source` and
  `kind`, the generated zone is not edited).
- `json-canvas` — writing and checking a JSON Canvas 1.0 `.canvas` by hand,
  and moving the Cerebro in and out of one with `brain_canvas_export` and
  `brain_canvas_import`.
- `fresh-context-review` — the session that drafted a document reviews it
  through `delegate_agents` read-only workers started without the
  conversation or shared context, one angle each, then triages every
  finding; repeated with new workers until no blocker or major finding is
  left.

## Format

Every skill here uses the exact dialect `services/memory/skill_format.py`
reads: YAML frontmatter (`name`, `description`, `version`, `category`,
`tags`, `status: published`, `source: imported`) followed by four sections —

```markdown
## When to Use
## Procedure
## Pitfalls
## Verification
```

— in that order, nothing else required. `description` always ends with an
explicit "Use when …" clause: that is the trigger a local model reads before
deciding whether to pull the rest of the procedure into context, so it has
to be concrete rather than a restatement of the name.

Each file targets 500–1200 words (hard cap 1500): these get injected into a
small model's context window, so density beats completeness. A skill that
wants to say more links to the library README or a sibling skill by name
rather than growing past the cap.

## Installing one

A skill here is inert until a user installs it — nothing runs, and nothing
is granted a tool, just by shipping in this folder. Installing (`GET /api/
skills/library`, `POST /api/skills/library/install`, or the Skills tab in
the UI) copies the procedure and its supporting text files into that user's own skill store
(`data/skills/<category>/<slug>/SKILL.md`) tagged `library:<slug>`, runs the
same static pre-scan any other imported skill folder gets
(`src.skill_import_review.scan_skill_folder`), and refuses outright if that
scan comes back CRITICAL. From that point on it is an ordinary user skill:
retrieved by relevance, surfaced in the system-prompt index, editable,
deletable — the library copy underneath is never read again once it is on
disk for that user.

## Categories

- **engineering** — coding habits and per-language patterns (Python, React,
  FastAPI, Go, Rust, Docker, migrations, deployment).
- **testing** — TDD, verification loops, end-to-end testing.
- **security** — security review checklists.
- **research** — search-first habits, deep research, documentation lookup.
- **planning** — intent-driven development, rules distillation, evaluation
  harnesses, prompt optimisation, multi-perspective deliberation, context
  budgeting, sub-agent orchestration.
- **writing** — prose and note formats: `obsidian-markdown`, `json-canvas`.

See `src/skill_library.py` for the module that lists and installs these, and
`routes/skill_library_routes.py` for the API.

## Skills carried by the family's apps

An app of the family can carry skills from this folder for the agents that
connect to it. Its manifest names the ones its work needs (`skills`: slugs,
tags, categories, limit); the app keeps its own copy, names them in what an
agent reads on connecting, serves them with `skills_list` and `skill_read`,
and offers them as prompts. A skill improved here reaches those copies on
their next refresh. `game-scenery-remodel` is the procedure for the
remodelling studio: taking a game model apart, choosing what each piece is
rebuilt as, comparing against the original and correcting.

## Procedures for the family's apps

Thirty procedures cover the everyday work of the family's apps: decks from a brief, company dossiers, recipes and menus,
photo curation, scheduling with constraints, discoveries, engagements, household inventory, exam plans, job
applications, paperwork and deadlines, the household ledger, read-later digests, plate heat and stress checks, data
cleaning, follow-ups with people, fine-tuning and evaluation, voxel models, electronics checks, team and battle
analysis, manuscript continuity, print-ready models, consistent characters, model serving on a small cluster, screen
capture, shared project files, catalogue and sales metrics, game libraries, personal context and depth cameras. Each
names the app whose tools it drives (`apps`), so it reaches that app and no other unless asked for by name.
