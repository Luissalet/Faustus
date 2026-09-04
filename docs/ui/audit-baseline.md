# Audit baseline (UI-012)

Date: 04-09-2026. Branch: `feat/studio-ui`.

Reference used: `vercel-labs/agent-skills@web-design-guidelines`, commit
`063bee94c3f4df8453406c830b0a7df0f2860278` (2026-08-28), read from the local
clone at `D:\LocalAI\_vercel_agent_skills_research`. Pinning the commit
matters: the guideline is a living list, and "we passed the audit" means
nothing without saying which audit.

## The rule

CI fails on **new** debt only. Legacy debt is cleared by the screen that
migrates over it, not in one heroic pass — a 1.52 MB stylesheet rewritten
in a single change is unreviewable, and mixing it with feature work is how
a migration stalls.

Enforced by `tests/test_studio_guards.py`, which scans `studio/src`:

| Guard | Rule |
|-------|------|
| `test_no_transition_all` | no `transition: all` |
| `test_no_outline_none` | no suppressed focus outline |
| `test_no_interactive_div` | no `<div onClick>` — use a real `<button>` |
| `test_no_inline_svg` | icons come from `lucide-react` |
| `test_colours_come_from_tokens` | no literal colour outside the token files |
| `test_durations_come_from_tokens` | no ad-hoc duration |
| `test_reduced_motion_is_handled` | a file that animates must handle reduced motion |
| `test_studio_tree_exists` | the guards fail loudly rather than scan nothing |

The guards are static and dependency-free on purpose: a rule that needs a
toolchain to run is a rule that quietly stops running.

### Exemptions

A line carrying `guard-ok:` plus its reason is exempt. The reason lives on
the line, so it is reviewed in the diff where it applies rather than in a
config file nobody opens. Two exist today:

- `components.css` — `outline: none` on `.fs-menu__item`: Radix owns roving
  focus there and paints it through `data-highlighted`, which mouse and
  keyboard share. A native ring would double up on the same item.
- `base.css` — `0.01ms` durations inside the `prefers-reduced-motion` block.
  That is the idiom itself.

## Legacy debt, measured today

Counted in `static/style.css` on 04-09-2026:

| Pattern | Count |
|---------|-------|
| `transition: all` | 77 |
| `outline: none` | 107 |
| Inline `<svg>` in `index.html` | 262 |

The plan quoted ~92 and >110 from an earlier sweep across more files. The
numbers above are what a direct count of `style.css` gives now, and they
are the ones to compare against as screens migrate. Neither figure moves
in this ticket: nothing in the legacy tree was touched.

## What these guards do NOT cover

Honest gaps, so nobody reads a green suite as more than it is:

- No colour-contrast check at runtime. The ratios in `DESIGN.md` were
  computed by hand and verified in the browser; an automated check over the
  rendered page belongs with the accessibility pass in UI-021.
- No check that an icon-only control has an accessible name. `IconButton`
  makes `label` a required prop, so the type system covers the primitive,
  but a raw `<button>` with only an icon would slip through.
- No keyboard-trap or focus-order verification. That needs the rendered
  page and arrives with the Playwright accessibility pass.
