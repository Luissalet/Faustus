---
name: web-accessibility-design
description: Web UI/UX and accessibility rules (WCAG 2.2, semantic HTML, keyboard/focus, forms, responsive layout, motion, contrast) for building or reviewing web interfaces. Use when writing or reviewing HTML/CSS/JS UI, auditing accessibility, or deciding how a web page should behave for keyboard, screen-reader and reduced-motion users.
version: 1.0.0
category: engineering
tags: [web, accessibility, wcag, ui, ux, frontend]
status: published
source: imported
---

## When to Use

Building or reviewing any web-facing UI: a page, a component, a form, a
Studio panel. Also use it as a checklist before calling a web feature
"done" — most of what it catches (missing labels, keyboard traps, contrast,
motion that never turns off) is invisible until someone who does not use a
mouse, or does not want animation, or reads with a screen reader, hits it.
Not needed for a pure backend/API change with no rendered output.

## Procedure

1. **Reach for semantic HTML before ARIA.** `<button>`, `<nav>`, `<main>`,
   `<dialog>`, `<fieldset>`/`<legend>` already carry the right role, keyboard
   behaviour and focus handling — ARIA is a patch for when no native element
   fits, not the default.
2. **Give every interactive element a real accessible name and a working
   keyboard path.** An icon-only control needs `aria-label`; visible text
   must never be overridden with a contradicting `aria-label` ("label in
   name", SC 2.5.3). Tab through the whole interaction by hand — a
   mouse-only test misses more than any other single check here.
3. **Check contrast and focus visibility together.** 4.5:1 for normal text,
   3:1 for large text and UI components, and a focus indicator still
   visible after your CSS reset — `outline: none` with nothing in its place
   is the single most common accessibility regression.
4. **Build the layout mobile-first and verify it at 320px with no
   horizontal scroll**, then layer breakpoints where the CONTENT breaks,
   not at device names. Touch targets at least 24x24 CSS px (44x44 where
   affordable), with real spacing between them.
5. **Label and validate forms the way a person actually fills them in**:
   a real `<label>` (never `placeholder` alone), `autocomplete` on common
   fields, validation on blur/submit rather than every keystroke, and an
   error message linked to its field with `aria-describedby`.
6. **Wrap every animation in a `prefers-reduced-motion` fallback** and
   restrict what you animate to `transform`/`opacity` so it stays smooth
   and can be turned off without a redesign.
7. **Theme with CSS custom properties, not a second stylesheet**, so light,
   dark, and `prefers-contrast: more`/`forced` all read from the same
   token set and can't drift out of contrast independently.
8. **Consult the reference table below for the specific rule that
   applies** before inventing your own convention for it.

## Pitfalls

- Adding `aria-label` to a button that already has clear visible text, with
  a DIFFERENT string — breaks voice-control users, who speak the visible
  label to activate a control (WCAG 2.5.3).
- Removing `outline: none` on focus without a visible `:focus-visible`
  replacement.
- Using `<div onclick>` where `<button>` would give keyboard support, a
  role and a focus state for free.
- Hiding essential content or actions behind `:hover` only — there is no
  hover on a touchscreen; pair it with `:focus-within` or a click handler.
- Disabling a submit button instead of validating on submit and showing
  errors — a disabled control with no explanation tells nobody why they
  are stuck.
- Hard-coding `margin-left`/`padding-right` instead of the logical
  (`margin-inline-start`/`padding-inline-end`) properties, which silently
  breaks the moment the page needs to support an RTL language.
- Treating `prefers-contrast: more` (stronger token colors) and
  `prefers-contrast: forced` (system color keywords) as the same thing —
  they need different CSS.

## Verification

- Tab through the whole page/component with no mouse; every interactive
  element is reachable, in a sensible order, and shows a visible focus
  ring.
- Run a contrast check on the actual token values, not "looks fine" —
  4.5:1 body text, 3:1 large text and UI components.
- Resize to 320px width: no horizontal scrollbar, nothing clipped.
- Toggle `prefers-reduced-motion` and confirm animations shorten to
  effectively nothing rather than looking broken.
- Toggle dark mode / `prefers-contrast: more` and re-check contrast — dark
  mode alone commonly fails a ratio light mode met.
- If the page has forms: submit it empty and confirm every error message
  is both visible and announced (`role="alert"`/`aria-live`).

## Reference: condensed rule table

Condensed and adapted from an MIT-licensed web design-rules pack (see
`NOTICE` in this folder for the license text and source). Framework-
agnostic; based on WCAG 2.2 and current web platform APIs. SC = WCAG
Success Criterion.

| Area | Rule |
|---|---|
| Accessible names | Prefer visible text; `aria-label`/`aria-labelledby` only when none exists (SC 4.1.2). Icon-only controls need `aria-label`. |
| Keyboard | Custom widgets: `tabindex="0"` (never positive) + keydown handling; a modal traps and then restores focus (SC 2.1.1). |
| Focus indicator | Never remove without a `:focus-visible` replacement with 3:1 contrast against adjacent colors (SC 2.4.7/2.4.11). |
| Skip link | A visually-hidden-until-focused "Skip to main content" link before the nav (SC 2.4.1). |
| Alt text | Informative images describe content/function; decorative get `alt=""`; functional images (in a link/button) describe the action (SC 1.1.1). |
| Contrast | 4.5:1 normal text, 3:1 large text (>=24px, or >=18.66px bold) and UI components. Never color alone for meaning (SC 1.4.1/1.4.3/1.4.11). |
| Live regions | `aria-live="polite"` for routine status; `role="alert"`/`assertive` only for time-sensitive warnings (SC 4.1.3). |
| ARIA roles | Prefer the native element; use a role only when no HTML element covers it. |
| Responsive | Mobile-first `min-width` queries; `clamp()`/`min()`/`max()` over hard breakpoints; container queries for component-scoped layout; never `user-scalable=no` (SC 1.4.4); no scroll at 320px (SC 1.4.10). |
| Forms | `autocomplete` on common fields; correct `type`/`inputmode` for the right mobile keyboard; `<fieldset>`/`<legend>` for groups; mark required (or optional, if most are required) (SC 1.3.5). |
| Typography | System font stack or `font-display: swap`; `rem` for sizes (never bare `px`); line-height >= 1.5 (SC 1.4.12); strict `h1`-`h6` order, one `h1`. |
| Performance | `loading="lazy"` below the fold, explicit `width`/`height` on images, `preconnect`/`preload` for critical assets, code-split by route, batch DOM reads then writes. |
| Motion | `prefers-reduced-motion: reduce` fallback (SC 2.3.3); animate only `transform`/`opacity`; never flash >3/sec (SC 2.3.1). |
| Theming | CSS custom properties per palette; `<meta name="color-scheme">`; re-verify contrast in dark mode independently; `forced` contrast wants system color keywords, `more` wants stronger tokens. |
| Navigation | Real bookmarkable URLs (`pushState`/`URLSearchParams`); handle `popstate`; `aria-current="page"` on the active link. |
| Touch | `touch-action` scoped to the gesture; pair every `:hover` with `:focus-visible`; `scroll-snap-*` for carousels. |
| i18n | `lang` on `<html>`; `dir="auto"` for user text; `Intl.*` formatters; CSS logical properties instead of physical ones; never bake text into an image. |
| PWA | `manifest.json` with icons at 192/512, `theme_color`/`background_color`, and a service worker with a `fetch` handler — all four together, or no install prompt. |
