---
name: hallmark-page-design
description: Design a NEW website or page so it looks made, not generated — a non-negotiable floor against model default habits, one question round about material, audience, use and tone, a chosen shape and direction stated once, hero first, then a sweep against the floor. Use when the user asks to build or design a new landing page, site, or page from scratch; for an existing design only when the user explicitly asks for a Hallmark redesign or audit.
version: 1.0.0
category: engineering
tags: [design, web-design, landing-page, frontend, css, ui, typography, layout, hallmark, diseno-web, pagina]
status: published
source: imported
---

## When to Use

Building a new page or site: landing, product page, portfolio, event, docs home. Not for edits to an existing design unless the user names Hallmark ("hallmark redesign this", "hallmark audit"); any other change to an existing design is done as asked, in the project's own style. For de-sloping an existing app screen see `unslop-interface`; for the motion inside the page see `gsap-core`.

Adapted from Nutlope/hallmark (MIT, pinned 52328cb3129135); its theme library and lint scripts are not bundled. See THIRD_PARTY_NOTICES.md.

## Procedure

1. Pre-flight: read the project before asking. A `DESIGN.md`/`design.md` at the root is a locked system and overrides everything below. Reuse the project's stack, token names, fonts and real brand assets; never invent a real brand's look from memory.
2. Ask once, in one message: material (screenshots, photos, real numbers, quotes with names — or a URL/folder you can run and capture), audience, use (the one job a visitor comes to do: buy, book, sign up, download…), tone. Skipped answers are inferred and stated in one line; facts are never inferred — a number, quote or logo not given is absent.
3. Shape the page: inventory the real material, name the visitor's job, decide the sections (six to nine, hero and footer included), the hero posture, the nav and the footer, and one signature — the strongest real thing shown at its best.
4. Direction: a named style the user asked for, a custom look derived from the material, or one strong direction you choose and justify in a sentence. A theme sets material (palette, faces, texture), never content or order.
5. State the picks once as a short block (shape, direction, hero, signature, sections, material real/pictures/absent) so the user can redirect before code.
6. Build tokens first: every colour and font as a custom property on `:root` (and a dark-scheme block if needed). Then the nav and hero only; check the hero at 1280×800 (headline ≤ 7 words, one sentence ≤ 12 words, one or two actions, all above the fold); then the remaining sections.
7. Show, do not describe: real screenshots of the product (capture them when it can be run), the user's photos, or the product rebuilt faithfully in HTML; a stand-in slot of the right size is better than a fake.
8. Page invariants: one primary button style, one container width capped and centred, one column gap below the hero, one radius per nesting level, body text 16 px or larger, two type families at most, eight states on every control.
9. Sweep against the floor below, fix every violation, then end with the material ask: each empty slot, its size, and the one piece of material that would most improve the page.

## Pitfalls

The floor — never waived:
- A literal colour or `font-family` outside the token block.
- `transition: all`, or animating `width`, `height`, `top`, `left`.
- One italic word inside a roman heading.
- `overflow-x: hidden` or `100vw` on `html`/`body` (use `overflow-x: clip`); image grid tracks without `minmax(0, 1fr)`; display heads without `overflow-wrap: break-word; min-width: 0`.
- An interactive element without an instant `:focus-visible` style; motion without a `prefers-reduced-motion` fallback; content invisible with scripting off.
- Invented metrics, testimonials, logo walls or case-study counts.
- Eyebrows and tracked all-caps or small-caps labels; labels, captions, table heads and nav links use the body face, a step smaller, sentence case.
- Middle-dot meta strings, arrows glued to link text, decorative status dots.
- Purple-to-blue gradients, a card inside a card, emoji as icons, hand-drawn browser or OS windows.
- More than about 80 words on one screen; a container whose measure grows at 1920 px.

## Verification

- Screenshot the page at 1280×800, 390×844 and 1920×1080 (dev server via `process_start`, then `desktop_screenshot` or a browser capture) and inspect each with `inspect_image`: hero complete above the fold, no horizontal scroll, gutters grow but the measure does not.
- Grep the CSS for hex/rgb literals and `font-family` outside the token block, `transition: all`, `text-transform: uppercase` with `letter-spacing`, and `100vw`.
- Tab through the page and confirm every control shows focus; toggle reduced motion.
- When the Vitruvius design critique tools are installed (`lookup_tools`), run them on the screenshot and fix or answer each finding.
- Report the picks block again with what the build changed and the sweep outcome; a fabricated pass is itself a failure.
