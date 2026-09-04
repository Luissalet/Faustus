# Visual critique of the current Faustus UI

Date: 04-09-2026. Branch: `feat/studio-ui`.
Method: `redesign-existing-projects` audit applied to baseline screenshots
(`docs/ui/baseline/`) and source (`static/index.html`, `static/style.css`,
`static/app.js`).

## Design read

| Axis | Current state |
|------|---------------|
| Framework | Vanilla HTML/CSS/ES modules, no build |
| Styling | Monolithic `style.css` (1.52 MB, 44 682 lines), CSS custom properties |
| Theme | Five public vars (`--bg`, `--fg`, `--panel`, `--border`, `--red`) + custom user themes |
| Fonts | Inter, Fira Code, OpenDyslexic (self-hosted WOFF2) |
| Icons | 262 inline SVGs, no library |
| Layout | Sidebar 224 px + main area, modals overlay for every tool |

## Audit findings

### Typography

- **Inter is the right foundation**, but only Regular (400) and Bold (700)
  are used in practice. Medium (500) and SemiBold (600) are missing from
  the hierarchy. Headlines and section titles look flat.
- **No display treatment.** The "Faustus" brand heading on the welcome
  screen uses the same weight and tracking as body text, just larger.
  No negative letter-spacing, no intentional typographic presence.
- **Body text has no max-width.** Chat messages stretch to fill the main
  area. At 1400 px, lines reach 100+ characters — well past the 65-char
  comfort zone.
- **Labels and metadata use the same size as body.** The `0.13s` timing
  label under model responses, the `69.15 tok/s` stat, and the turn
  summary line all compete at similar sizes. The hierarchy is flat.
- **Tabular numbers not enforced.** Timing, token counts, costs and
  progress percentages use proportional figures. Columns of numbers
  jitter.

### Color and surfaces

- **`--bg: #282c34` is a cold gray**, adequate but generic (One Dark
  palette origin). `--panel: #111` is nearly pure black — too dark for a
  secondary surface. The jump from `#282c34` to `#111` is 60% lightness
  drop with no intermediary.
- **`--fg: #9cdef2` is a saturated cyan.** As the main text color, it
  tints everything aqua. It works as a brand accent but not as a reading
  surface. Contrast against `#282c34` is ~6.1:1 (passes AA) but the hue
  fatigue is real over long sessions.
- **`--red: #e06c75` (coral) is used for brand and error.** The
  `--color-error: #ff4444` and `--color-danger: #c0392b` are separate
  reds. Three different reds, and the brand coral overlaps with error
  semantics.
- **`--color-agent-active: #00ff00` is pure neon green.** Fully saturated,
  clashes with every other color. The success green (`#4caf50`) is a
  different hue. Two greens, one of them painful.
- **No surface ladder.** The system has `--bg` and `--panel` — two levels.
  Linear uses four levels; Raycast uses four. Faustus has two, and they
  are too far apart in lightness.
- **Shadows are absent.** Elevation is conveyed only by background color
  change. The modals have no shadow, no border treatment. They appear as
  flat rectangles over a dimmed backdrop.
- **Light mode is minimal.** `--bg: #f5f5f5`, `--panel: #fff` —
  functional but bland. No warm tint, no surface progression, no
  considered light palette.

### Layout

- **18+ sidebar entries** compete for attention. The sidebar is a
  directory of subsystems, not a navigation of user intentions. A new
  user scanning Brain, Calendar, Compare, Tournament, Cookbook, Deep
  Research, Experts, Agent runners, Agent definitions, Imported history,
  Provenance, Gallery, Library, Notes, Tasks, Workers, Theme... cannot
  form a mental model.
- **Everything is a modal.** Projects, Gallery, Library, Settings — all
  open as modals or panels with no URL, no browser back, no bookmarking.
  The app has one real route (`/`) and hash-based session switching.
- **The composer sits at the bottom of a vast empty space.** On the
  welcome screen at 1400×900, the brand logo and "New chat ready" float
  in the vertical center, and the composer is at the bottom. The space
  between is empty — no quick starts, no recent work, no pending
  approvals.
- **Mobile sidebar is a scroll-to-find experience.** At 390×844, the
  user must open the hamburger and scroll through all 18+ entries.
  There is no bottom tab bar, no reduced navigation.
- **No max-width on the main content area.** Chat messages stretch
  edge-to-edge minus sidebar. At ultrawide resolutions, the UI becomes
  uncomfortably wide.

### Interactivity and states

- **92 instances of `transition: all`** — the most expensive and
  unpredictable transition. Any property change triggers animation,
  including layout shifts.
- **110+ `outline: none` without focus replacement.** Keyboard users
  lose their position. This is the most serious accessibility debt.
- **No skip-to-content link.** Keyboard navigation starts at the
  sidebar top and must tab through every entry.
- **Interactive divs.** Menu items and toolbar entries are `<div>` with
  click handlers and `tabindex="0"`, but no `role="button"` or
  `role="menuitem"`. Screen readers cannot announce them correctly.
- **No empty states with actions.** The Gallery opens empty with no
  guidance. The Library opens empty. The only empty state with a clear
  CTA is the Projects page ("Create your first project").
- **No loading skeletons.** Content appears after a flash of empty space.
- **Hover states exist but are inconsistent.** Some sidebar items
  highlight on hover, others don't. The model selector has a hover state;
  the Agent/Chat toggle has one.

### Content

- **"New chat ready" is the welcome message.** It tells the user
  the system is ready but not what to do. No continuations, no pending
  approvals, no quick starts. The tip ("Right-click a session for rename,
  delete, and memory options") is a power-user hint, not guidance.
- **"Nobody" persona selector** is visible on the welcome screen without
  explanation. What does selecting a persona do? No tooltip, no help.
- **GPU bar at the top** (`GPU 3% · 1.5/28G · 48° · ollama offline ·
  RAM 32%`) is infrastructure information taking prime real estate. It
  matters when something is wrong; otherwise it's noise.
- **Temperature and model selector** (`T auto`, `Select model`) are
  visible before project, skill, or purpose. The user sees technical
  dials before creative intent.

### Component patterns

- **Cards are rare.** The UI is mostly flat lists and text blocks.
  The approval card is the one well-structured card component.
- **The approval card is the best component.** Three-tiered actions,
  clear context, honest metadata. This should be the design standard.
- **Tool call nodes** (`READ_FILE done`, `EDIT_FILE done`) use a
  minimal inline format that works well for technical users.
- **The verified card** with claims verification is excellent: clear,
  auditable, actionable (Restore / Commit).

### Iconography

- **262 inline SVGs** with inconsistent stroke widths, sizes, and
  padding. Some are 13 px, some 14 px, some 15 px. Stroke weights vary
  between 1.6 and 2.5.
- **Icon opacity is 0.5** on sidebar tool items — a reasonable choice
  for secondary items but applied uniformly, making everything feel
  half-hearted.

---

## Anatomy references consulted

### From Linear (surface ladder, density, restraint)

**Taken:**
- The four-step surface ladder concept: canvas → surface-1 → surface-2 →
  surface-3. Faustus needs at least four levels instead of two.
- Aggressive negative tracking on display text. The Faustus brand heading
  should feel deliberate, not default.
- Product screenshots as hero visuals. In Faustus, the "product" is the
  work itself — the run timeline, the diff, the artifact. These should
  dominate, not be buried in chat scroll.
- Dense, functional chrome for product contexts (code, data, activity).
- Hairline borders (1 px) as the primary separator, not cards or shadows.

**Not taken:**
- Linear's lavender accent. Faustus has coral (`#e06c75`) as brand.
- Linear's dark-only stance. Faustus has light mode and custom themes.
- Linear's marketing focus. This anatomy is for a product UI, not a
  landing page.

### From Raycast (command palette, keyboard-first, surface elevation)

**Taken:**
- Elevation built entirely from surface-color ladder and 1 px borders,
  not drop shadows. Faustus should reserve shadows for overlays and drag.
- Command palette as hero interaction. Faustus's Ctrl+K will become the
  primary navigation tool.
- Tight radius vocabulary (4–16 px). No pills on cards, pills only for
  semantic indicators (status badges, tags).
- Keycap glyphs for keyboard shortcuts. Faustus is keyboard-heavy; the
  shortcuts should be visible and consistent.
- Reserved saturated colors: only in illustrations or state indicators,
  never on chrome or text.

**Not taken:**
- Raycast's dark-only, developer-exclusive aesthetic. Faustus serves
  both technical and creative users.
- White pill as universal CTA. Faustus uses coral as brand accent for
  primary actions.
- The ss03 Inter variant. Inter standard is fine for Faustus; the
  brand differentiation comes from the warm/editorial tone, not a
  single glyph.
- Red diagonal stripe as hero decoration. Faustus should not borrow
  signature decorative elements.

---

## What the overhaul must fix (priority order)

1. **Surface ladder:** four levels with consistent warm tint, replacing
   the `#282c34` / `#111` cliff.
2. **Text color:** neutral high-contrast text instead of saturated cyan
   for reading. Cyan can stay as a decorative accent.
3. **Sidebar collapse:** 18 entries → 5-6 destinations with progressive
   disclosure. Tools live behind context, not primary navigation.
4. **URL-based routing:** every destination is a real route. Browser back,
   bookmarks, and sharing work.
5. **Typography scale:** display / title / body / label / code with
   distinct weights and tracking.
6. **Focus visibility:** replace every `outline: none` with a visible
   focus ring. Add skip link.
7. **Context bar:** project, skill, backend, cost visible before sending.
8. **Empty states:** every panel has guidance and a primary action.
9. **Transition cleanup:** kill `transition: all`, specify properties.
10. **Icon consistency:** standardize on `lucide-react` (MIT) with
    consistent size (16 px) and stroke (2 px) in Studio components.

## What to keep

- The coral brand color (`#e06c75`) is distinctive and warm. It should
  not be confused with error; error gets its own red.
- The approval card and verified card are well-designed. Their pattern
  (context + options + metadata) should inform other interactive cards.
- Inter + Fira Code + OpenDyslexic as a font stack. No remote fonts.
- Custom user themes via CSS variables. The new token layer must alias
  backward to `--bg`, `--panel`, `--fg`, `--red`.
- The agent harness nodes (read, write, checkpoint, verified, queued)
  are honest and compact. They should evolve, not be replaced.
