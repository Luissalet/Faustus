---
name: unslop-interface
description: Turn a generic, AI-looking interface into a designed one that only this product could have — audit the signs, set a five-line direction from the product, then fix spacing, colour, icons, copy, type, layout, details, visuals and motion in that order, keeping features, content and section order. Use when an existing app screen, dashboard or landing looks vibecoded, generic or "AI slop" and the user wants it to look designed.
version: 1.0.0
category: engineering
tags: [design, ui, ux, redesign, polish, dashboard, landing-page, css, tailwind, unslop, slop, diseno, interfaz]
status: published
source: imported
---

## When to Use

An existing screen glows everywhere, puts emoji in labels, uses random spacing, shouting copy, purple gradients and floating blobs, or simply could belong to any product. The job is a redline pass that fixes craft and keeps the product: features, content and section order stay. For a brand-new page see `hallmark-page-design`.

unslop by Damir Sokolovsky (@heydamir, https://github.com/heydamir/unslop), licensed CC BY 4.0. This is an adapted, condensed version (changed wording, Faustus tools, no bundled scripts or reference images); see THIRD_PARTY_NOTICES.md.

## Procedure

1. Look at the screen as people see it: find the files that render it, run the app (`process_start`) and take a screenshot. Only a screenshot available: rebuild it as one static HTML file and treat that as the original.
2. Audit: list every sign of slop, one line each with where it lives (glows, gradients, emoji icons, tracked uppercase labels, off-grid spacing, hype copy, fake numbers, looping motion).
3. Decide how much to change: heavy slop → full process; light slop → polish only what the audit found and keep palette, type and layout; no slop → say so and offer two or three exact refinements at most.
4. Write the direction and stop for the user's answer (`ask_user`): Product (what and for whom), Character (two or three concrete words; not clean, modern, sleek, minimal or premium), World (the place or material it belongs to, which tints neutrals and accent), Moments (one or two places that get rich colour and detail), Signature (one small detail only this product would have). Keep a real brand; gradients and sparkles are not brand.
5. Fix in nine passes, each across the whole screen before the next: (1) spacing on a 4 px grid, equal things equal, smaller gaps inside groups than between them; (2) colour from one tinted neutral ramp, three text roles, one rare accent with 4.5:1 contrast, no hue gradients, glows or coloured shadows, hairline borders; (3) icons from one set at one size and stroke, coloured by their text role, emoji removed; (4) copy that says what the product does for whom, no exclamation marks, no greetings, no hype, no unverifiable claims, plausible mock data; (5) type chosen for the character, weights 400–500 (600 rarely), sentence case, tabular numbers, no italic emphasis; (6) layout keeps sections and order, real app proportions, an asymmetric hero or a scene with text on calm space, one primary and one secondary action; (7) details: nested radii that add up, keycaps for shortcuts, quiet counts, one accent per chart, stat cards that are just numbers; (8) visuals: remove decorative filler, show the real product or a scene from the world; (9) motion: remove loops, bounces, hover scaling and `transition-all`; keep short motion that explains.
6. Use `generate_image` only for a landing hero scene or empty states, started right after the direction is confirmed; otherwise build the product view in code and leave a correctly sized slot with the prompt.
7. Re-render and compare with the audit list; anything left is fixed. Render at most three times.
8. Report the direction, a few lines per pass, and any claims removed or needing confirmation.

## Pitfalls

- Renaming the product or changing what it is; inventing features, claims or data.
- Replacing removed decoration with new decoration, or swapping one trend for another (glass, grain, neon, bento grids).
- The same look for every product: the direction, not habit, sets tint, type and visual.
- Coloured app backgrounds: in apps and dashboards surfaces stay neutral and content carries colour.
- Negative letter spacing on small text; 700–900 weight headings; gradient text.
- Animating width, height, top, left, margins or large blurs.

## Verification

- Checklist: direction written and followed; spacing on the 4 px scale; icons, labels and numbers aligned in columns; one neutral ramp, three text roles, one accent with enough contrast; no gradients, glows or emoji; one icon set; no uppercase tracked labels; no exclamation marks or unverified claims; tabular numbers in one format; nested radii add up; no looping or bouncy motion.
- Before/after screenshots inspected with `inspect_image`, at desktop and mobile widths.
- Template check: swap the product name for another product's — if the screen still fits, push the direction harder in copy, visual and signature.
- Shared outputs (videos, story pages) carry the credit "unslop by @heydamir".
