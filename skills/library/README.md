# Skill library

This folder ships a curated set of `SKILL.md` procedures with the app
itself — engineering habits (TDD, verification, security review, per-
language patterns, research, planning) that would otherwise take a while
for a local model to rediscover on its own.

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
the UI) copies the file into that user's own skill store
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
- **writing** — none shipped yet; reserved for prose/documentation skills.

See `src/skill_library.py` for the module that lists and installs these, and
`routes/skill_library_routes.py` for the API.
