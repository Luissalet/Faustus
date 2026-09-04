# Faustus Studio — Design system

Authority: this document is the single source of truth for color, typography,
spacing, motion and component states in any UI work on `feat/studio-ui`.
`DECISIONES_UI.md` §5 declares it a hard blocker: no ticket from UI-010
onward starts without these tokens.

Visual direction: **editorial, warm, technical.** A personal studio for
finishing work — precise and fast in code and data, wide and visual in media,
comprehensible to someone who does not know the internal subsystems. Not SaaS
blue, not cyberpunk, not a clone of Linear, Raycast or any other product.

## Design dials (per Taste Skill vocabulary)

| Context | Variance | Motion | Density |
|---------|----------|--------|---------|
| Base | 5 | 3 | 6 |
| Inicio / onboarding | 6 | 4 | 3 |
| Studio creative | 6 | 4 | 5 |
| Studio code | 3 | 2 | 8 |
| Library visual | 5 | 3 | 6 |
| Automations / Activity | 3 | 2 | 8 |
| Mobile | base − 1 | base − 1 | base − 1 |

---

## Color

Every color below has been tested against every surface in its theme.
All text-on-surface pairs pass WCAG AA (4.5:1 minimum).

### Dark theme

```css
/* Surfaces — four-step ladder, warm charcoal */
--fs-canvas:        #0c0d0f;
--fs-surface-1:     #121418;
--fs-surface-2:     #191c21;
--fs-surface-3:     #22262d;

/* Borders */
--fs-border:        #2b3038;
--fs-border-strong: #3b424d;

/* Text */
--fs-text-1:        #f2f1ed;   /* primary — warm off-white */
--fs-text-2:        #bdc1c9;   /* secondary */
--fs-text-3:        #8c929e;   /* muted — CORRECTED from #858b96 (was 4.43:1 on surface-3, now 4.86:1) */

/* Brand */
--fs-brand:         #e06c75;   /* coral — identity and primary action */
--fs-brand-hover:   #ec8189;

/* Functional */
--fs-focus:         #8ab4ff;   /* focus ring, links, active execution */
--fs-success:       #54aa78;
--fs-warning:       #d9a441;
--fs-danger:        #e46e76;   /* CORRECTED from #df626d (was 4.41:1 on surface-3, now 4.91:1) */
--fs-info:          #7198e5;

/* Ink on a coloured fill — added 04-09 after building the primitives */
--fs-on-brand:      #0c0d0f;   /* on coral: 6.09:1. White would be 2.85:1 */
--fs-on-danger:     #0c0d0f;   /* 6.22:1 */
--fs-danger-solid:  #c8323e;   /* destructive FILL only; white on it: 5.27:1 */
--fs-on-danger-solid: #ffffff;
```

### Light theme

```css
/* Surfaces — warm paper */
--fs-canvas:        #f3f1ec;
--fs-surface-1:     #faf9f6;
--fs-surface-2:     #ffffff;
--fs-surface-3:     #ebe8e1;

/* Borders */
--fs-border:        #d8d4cb;
--fs-border-strong: #bbb5aa;

/* Text */
--fs-text-1:        #242321;
--fs-text-2:        #55534e;
--fs-text-3:        #65615a;   /* CORRECTED from #77736b (was 3.86:1 on surface-3, now 5.03:1) */

/* Brand */
--fs-brand:         #b3424d;   /* CORRECTED from #c9515d (was 3.56:1 on surface-3, now 4.51:1) */
--fs-brand-hover:   #9c3843;   /* CORRECTED from #ad3f4a */

/* Functional */
--fs-focus:         #315fa8;
--fs-success:       #2d7a4f;
--fs-warning:       #946a1a;
--fs-danger:        #c0392b;
--fs-info:          #2c5bb5;

/* Ink on a coloured fill. The light brand is deep enough for white,
   which is the exact inverse of the dark theme — measured, not assumed. */
--fs-on-brand:      #ffffff;   /* 5.52:1. Ink would be 2.84:1 */
--fs-on-danger:     #ffffff;   /* 5.44:1 */
--fs-danger-solid:  #a4231b;   /* white on it: 7.41:1 */
--fs-on-danger-solid: #ffffff;
```

### Contrast corrections summary

| Token | Theme | Old value | New value | Worst pair | Old ratio | New ratio |
|-------|-------|-----------|-----------|------------|-----------|-----------|
| `text-3` | dark | `#858b96` | `#8c929e` | on surface-3 | 4.43:1 | 4.86:1 |
| `danger` | dark | `#df626d` | `#e46e76` | on surface-3 | 4.41:1 | 4.91:1 |
| `text-3` | light | `#77736b` | `#65615a` | on surface-3 | 3.86:1 | 5.03:1 |
| `brand` | light | `#c9515d` | `#b3424d` | on surface-3 | 3.56:1 | 4.51:1 |
| `brand-hover` | light | `#ad3f4a` | `#9c3843` | on surface-3 | — | 5.30:1 |

### Color rules

0. **Destructive is an outline, not a fill.** A solid danger button next
   to a solid brand button is the same red twice: "Nuevo trabajo" and
   "Eliminar" become interchangeable at a glance. This was found by
   building the gallery and looking at it, not by reading the spec. So
   `danger` renders as a 1px `--fs-danger` border with `--fs-danger`
   text, and `--fs-danger-solid` exists for exactly one case: the
   confirming button inside a destructive dialog, where it is that
   dialog's primary action and the brand fill is absent. The side effect
   is correct emphasis — deleting should never be the loudest thing on a
   screen.
1. Coral (`--fs-brand`) identifies the brand and primary action. It is
   never used for error state. Error gets `--fs-danger`.
2. Blue (`--fs-focus`) communicates focus, links and active execution.
3. Green, amber and red are reserved for semantic state.
4. No purple-blue gradients as decoration.
5. Custom user themes keep working via aliases:
   `--bg → --fs-canvas`, `--panel → --fs-surface-1`,
   `--fg → --fs-text-1`, `--red → --fs-brand`.

---

## Typography

No remote fonts. All faces are self-hosted WOFF2 already present in
`static/fonts/`.

| Token | Family | Fallback | Use |
|-------|--------|----------|-----|
| `--fs-font-ui` | Inter | `system-ui, -apple-system, sans-serif` | Navigation, controls, reading |
| `--fs-font-code` | Fira Code | `ui-monospace, 'Cascadia Code', monospace` | Code, paths, IDs, recipes |
| `--fs-font-a11y` | OpenDyslexic | `--fs-font-ui` fallback | User preference for dyslexia |

### Type scale

Only three Inter faces are self-hosted — Regular (400), Medium (500) and
SemiBold (600). The scale below is written against those. The 650 and 620
of the original plan do not exist as files: the browser rounds them to 600
and the document ends up describing a weight nobody ships.

| Token | Size | Weight | Line-height | Letter-spacing | Use |
|-------|------|--------|-------------|----------------|-----|
| `--fs-display` | 28–36 px | 600 | 1.15 | −0.025 em | Inicio and project headers |
| `--fs-title` | 20–24 px | 600 | 1.25 | −0.01 em | Screen or artifact titles |
| `--fs-body` | 14–16 px | 400 | 1.5 | 0 | Content, chat messages |
| `--fs-label` | 12–13 px | 500 | 1.4 | 0.01 em | Metadata, controls, timestamps |
| `--fs-code` | 13–14 px | 400 | 1.5 | 0 | Inline and block code |

The faces are declared again in `studio/src/styles/fonts.css` rather than
inherited from `style.css`, so the Studio bundle renders correctly on its
own. Same files, no new asset, no CDN.

### Typography rules

- Headlines use negative letter-spacing. Body does not.
- SemiBold (600) and Medium (500) exist in the hierarchy; avoid jumping
  from 400 to 700 only.
- `font-variant-numeric: tabular-nums` on costs, progress, timings and
  any columnar numbers.
- Paragraph `max-width: 65ch` for readable line length in content areas.
- `text-wrap: pretty` on multi-line headings to avoid orphans.
- OpenDyslexic is loaded only when activated by the user preference.

---

## Space

Base unit: 4 px. The scale is:

| Token | Value | Common use |
|-------|-------|------------|
| `--fs-space-1` | 4 px | Inline gaps, icon-to-label |
| `--fs-space-2` | 8 px | Tight padding, between related items |
| `--fs-space-3` | 12 px | Default component padding |
| `--fs-space-4` | 16 px | Section gaps, card padding |
| `--fs-space-5` | 24 px | Between sections |
| `--fs-space-6` | 32 px | Major layout gaps |
| `--fs-space-7` | 48 px | Top-level section separation |

Only these values. No arbitrary pixel sizes in Studio components.

---

## Radius

| Token | Value | Use |
|-------|-------|-----|
| `--fs-radius-control` | 6 px | Buttons, inputs, badges |
| `--fs-radius-panel` | 10 px | Cards, panels, popovers |
| `--fs-radius-preview` | 14 px | Image and video previews |
| `--fs-radius-pill` | 999 px | Semantic pills (status, tag) only |

Inner elements use tighter radii than their containers. Never the same
radius on a card and its child button.

---

## Elevation

Faustus is flat-first. Elevation is conveyed through the surface-color
ladder and 1 px borders, not by wrapping every block in a card.

| Level | Treatment | Use |
|-------|-----------|-----|
| Base | `--fs-canvas` background | Page canvas |
| Raised | `--fs-surface-1` or `-2` background, `--fs-border` 1 px border | Panels, sidebar |
| Overlay | `--fs-surface-3` background, `--fs-border-strong` 1 px border, shadow | Menus, popovers, dialogs |
| Drag | `box-shadow: 0 8px 24px rgba(0,0,0,0.32)` + scale(1.02) | Active drag handle |

### Shadow rules

- Shadows only on overlays (menus, dialogs, popovers) and drag.
- Never on inline cards or list items to "separate" them.
- Shadow color is tinted dark, not pure black: `rgba(0,0,0,0.32)` in
  dark mode, `rgba(0,0,0,0.08)` in light mode.
- Consistent light direction: top-left source (offset 0, positive Y).

---

## Motion

| Token | Duration | Easing | Use |
|-------|----------|--------|-----|
| `--fs-duration-fast` | 120 ms | `cubic-bezier(.2,.8,.2,1)` | Hovers, micro-feedback |
| `--fs-duration-normal` | 180 ms | `cubic-bezier(.2,.8,.2,1)` | Panel transitions, focus changes |
| `--fs-duration-slow` | 240 ms | `cubic-bezier(.2,.8,.2,1)` | Inspector open/close, route changes |

### Motion rules

1. **Never `transition: all`.** Specify properties: `transform`, `opacity`,
   `background-color`, `border-color`, `color`.
2. Every animation can be interrupted (no `animation-play-state: running`
   without pause support).
3. `prefers-reduced-motion: reduce` disables translation, parallax,
   loops and scroll-driven effects. Opacity fades stay, shortened to 0 ms.
4. Progress indicators may animate. A screen at rest does not move for
   decoration.
5. Stagger entries by 30–60 ms per item, max 5 items. Beyond that, use
   a single group fade.

---

## Density by context

| Context | Row height | Padding | Font | Description |
|---------|-----------|---------|------|-------------|
| Comfortable | 48 px | `--fs-space-4` | `--fs-body` | Inicio, creative studio |
| Default | 40 px | `--fs-space-3` | `--fs-body` | Projects, library |
| Compact | 32 px | `--fs-space-2` | `--fs-label` | Activity, code studio, data tables |

Touch targets are always at least 44×44 px on mobile, regardless of
visual density. `padding` expands the hit area where the visual row is
smaller.

---

## States

Every interactive component must implement these states where applicable:

| State | Visual treatment |
|-------|-----------------|
| **Default** | Base colors, no emphasis |
| **Hover** | `background-color` shift one surface step up; 120 ms transition |
| **Focus-visible** | `outline: 2px solid var(--fs-focus); outline-offset: 2px` — never removed |
| **Active / pressed** | `transform: scale(0.98)` or `translateY(1px)` for 120 ms |
| **Disabled** | `opacity: 0.4; pointer-events: none; cursor: not-allowed` |
| **Loading** | Skeleton with `aria-busy="true"`; no spinner loops in `prefers-reduced-motion` |
| **Empty** | Illustration or icon + heading + explanation + primary action CTA |
| **Error** | Inline message with `--fs-danger` icon + text; never `window.alert()` |
| **Success** | Brief inline confirmation with `--fs-success`; auto-dismiss after 3 s |
| **Selected** | `--fs-brand` left border or background tint + `aria-selected="true"` |

---

## Responsive breakpoints

| Token | Width | Layout |
|-------|-------|--------|
| `--fs-bp-desktop` | ≥ 1280 px | Nav 224 px + main flexible + inspector 320 px |
| `--fs-bp-tablet` | 768–1279 px | Rail 64 px + main; inspector as overlay |
| `--fs-bp-mobile` | < 768 px | Topbar + main + bottom nav (5 items max); inspector as bottom sheet |

### Mobile rules

- Touch area: minimum 44×44 px.
- Safe areas: `padding: env(safe-area-inset-*)`.
- No action depends solely on hover, drag or swipe.
- Composer stays visible with on-screen keyboard using `100dvh`.
- Overlay panels use `overscroll-behavior: contain`.
- Five most frequent actions fit without a menu; the rest goes to "More".
  This is a rule about **actions**. The six destinations plus the pilot's exit
  measure 44×44 each at 375 px and all fit in the bottom bar, verified in the
  browser — so navigation is not pushed behind a "More" menu it does not need.
- **Collapsing a label is never `display: none`.** The rail and the bottom bar
  hide their text visually, but the icons are `aria-hidden`, so removing the
  span from the accessibility tree leaves six links with no accessible name at
  all. Use the clip-path visually-hidden pattern; it costs nothing and it is
  the difference between a usable bar and an unusable one.

---

## Icon system

| Property | Value |
|----------|-------|
| Library | `lucide-react` (MIT, tree-shaken) |
| Size | 16 px standard, 20 px in headers, 24 px in empty states |
| Stroke | 2 px consistent |
| Color | `currentColor` — inherits from text |

The 262 inline SVGs in `index.html` are legacy. They stay untouched
and disappear when their screen migrates. No new inline SVGs in Studio.

---

## Font and icon licenses

| Asset | License | Source |
|-------|---------|--------|
| Inter | SIL Open Font License 1.1 | `static/fonts/` (self-hosted) |
| Fira Code | SIL Open Font License 1.1 | `static/fonts/` (self-hosted) |
| OpenDyslexic | Bitstream Vera license (permissive) | `static/fonts/` (self-hosted) |
| lucide-react | MIT | npm dependency (tree-shaken) |

No remote font loading. No CDN at runtime.

---

## Signature element — the execution trace

A design system without a signature is a set of defaults. Faustus gets
exactly one, and it is born from what the product does rather than from
decoration: **the execution trace**.

A thin vertical rail threading `context → steps → artifact`. It appears
wherever work happens — Studio, Actividad, and the artifact card — always
with the same anatomy, so a run reads the same way no matter which screen
you meet it on. It is the thing Faustus shows that the other workspaces
bury inside a chat transcript, which is precisely why it is the signature.

### Anatomy

| Part | Spec |
|------|------|
| Rail | 2 px, `--fs-border`; the travelled portion mixes 45% `--fs-brand` |
| Node | 10 px circle, 2 px border, coloured by state |
| Label | `--fs-body`; `--fs-text-2`, rising to `--fs-text-1` + 500 when the step is running or waiting; `--fs-text-3` when queued |
| Meta | `--fs-label`, `--fs-text-3`, tabular figures, right-aligned |
| Row | `--fs-space-2` block padding; 18 px rail column |

Each step paints the rail segment through its own row, so the line can
never desync from the nodes — the first and last steps paint half a
segment, which closes the rail at both ends without a wrapper element.

### The one animation with character

The running node carries a halo that breathes between 0.55 and 0.12
opacity over 2 s. **Opacity only** — never position, never size, never
colour. That is what lets `prefers-reduced-motion` stop it dead without
losing any information: state is still carried by the node colour, the
label weight and the status text.

### Collapsing

Beyond three consecutive finished steps, the completed run collapses into
a single openable line ("N pasos completados") drawn with a dashed rail,
so a twelve-step run does not push the active step off the screen. History
stays on the rail; it does not go somewhere else.

### What it must not become

- Not a log viewer. Tool output, commands and evidence live in the run
  detail, not on the rail.
- Not decoration. It appears only where there is a real run.
- Not a progress bar in disguise: a step that is waiting for a person says
  so, with the approval it needs.

## Character layer — atmosphere, glass and candy

Added 04-09 after two verdicts on the first shell: "el primo contable de
ChatGPT", then "le falta sauce, sobre todo en claro". Everything here is a
token in `tokens.css`, derived from the palette with `color-mix` so the guard
never sees a literal colour, and everything that moves stops under
`prefers-reduced-motion`. The test for candy: strip the layer and the app
still works — it just stops glowing.

### Tokens

| Token | Dark | Light | Used by |
|---|---|---|---|
| `--fs-ember`, `-strong`, `-faint` | brand 22 / 45 / 9 % | brand 26 / 50 / 12 % | headers, active node, user bubble |
| `--fs-glow` | 4 px ring + 24 px bloom | same | indicator, send button, running node |
| `--fs-aurora-a/b/c` | brand 30 %, info 26 %, warning 16 % | brand 42 %, info 34 %, warning 30 % | the three drifting blobs behind the shell |
| `--fs-aurora-blend`, `--fs-aurora-opacity` | `screen`, 0.7 | `multiply`, 0.8 | `.fs-aurora span` |
| `--fs-paper` | flat canvas | warm 160° gradient canvas → warning-tinted → brand-tinted | `.fs-shell` |
| `--fs-dots` | text 6 % | brand 16 % | dot grid `.fs-shell::after`, masked to fade bottom-right |
| `--fs-glass`, `--fs-glass-strong` | surface-1 62 %, surface-2 78 % | surface-2 66 %, 84 % | panels, tiles, composer |
| `--fs-highlight` | text 8 % | surface-2 90 % | the 1 px light along the top of glass |
| `--fs-panel-shadow` | inset highlight only | inset highlight + coral 9 % 30 px + ink 6 % 6 px | resting panels |
| `--fs-lift-shadow` | canvas 60 % 34 px | coral 16 % 44 px + ink 8 % 10 px | hover on tiles/cards, composer, send |
| `--fs-accent-gradient` | brand → brand-hover → warning | same colours, light values | primary button, send, mode thumb |
| `--fs-title-gradient` | brand → hover → warning → brand | same | the one gradient word per screen |
| `--fs-chamfer`, `-sm` | 14 / 9 px | same | clipped corners: the arrowhead of the mark |
| `--fs-ease-spring`, `--fs-duration-enter`, `--fs-stagger` | 420 ms / 45 ms | same | entrances and the sliding indicator |

### Why light needs its own recipe

Screen blending adds light; over near-white it adds nothing. Glows are the
same story. The light theme therefore multiplies (darkens) the aurora,
tints the canvas itself, and replaces glows with coloured shadows, which
are the only way a surface reads as lifted on paper.

### Rules

- One gradient word per screen (`.fs-home__title em`). Two is a poster.
- Glass only on surfaces that sit over the aurora; never on text.
- The signal (the travelling light) runs on the navigation rail and on the
  Studio header, and nowhere else: it marks the two spines of the app.
- Chamfers on tiles, the user bubble, the hero and the approval card.
  Buttons and inputs keep radii; chamfering a 28 px control makes it a bug.
- Spotlight (`.fs-spot`) on tiles and cards the pointer can hover; it does
  not exist on touch and must not be load-bearing.
- Nothing in this layer may be the only carrier of a state. Colour, icon
  and text still say what is running, waiting or failed.
