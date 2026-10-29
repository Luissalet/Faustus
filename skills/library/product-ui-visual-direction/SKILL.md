---
name: product-ui-visual-direction
description: Give a business web application a distinctive, non-template visual direction and purposeful motion without slowing daily work - surfaces, dials, tokens, signature moments, motion rules and a bounded verification pass with Vitruvius and the design detector. Use when designing or redesigning screens of a product UI that must look striking but stay fast and comfortable.
version: 1.0.0
category: engineering
tags: [design, ui, ux, motion, animation, tokens, design.md, vitruvius, impeccable, redesign, diseno, animaciones, interfaz, llamativo]
status: published
source: imported
---

## When to Use

A B2B or internal product (catalogue, configurator, quotes, project views)
must feel crafted and memorable while people use it all day. For a marketing
landing page alone, `hoard-design-loop` is enough; this procedure decides
where the striking parts go and keeps work screens quiet.

## Procedure

1. **Read the product truth first.** `PRODUCT.md` (audience, purpose,
   constraints) and `DESIGN.md` (tokens with the reason for each value). If
   they do not exist, write them before any screen. Extract the client's
   existing brand from its site stylesheet (colours, fonts, logo) and check
   font licences for web app and server-side PDF use; Adobe-hosted fonts
   usually do not cover either.
2. **Classify every surface and set three dials** (variance, motion,
   density):
   - showcase (catalogue, product pages, project stories): high, medium, low;
   - specialist viewer (3D, timelines, maps): medium, purposeful, medium;
   - work screens (tables, forms, documents): low, minimal, high.
3. **Pick a direction from the domain, not a template.** Look for a motif in
   the client's material or process (layers, drawings, joints, grids) and
   turn it into rules: grid unit, rule weight, loading pattern, icon stroke.
   Use one accent colour for interaction only; check what competitors use and
   avoid their colour. Pair a characterful sans with a tabular monospace for
   every number.
4. **Write the Design Read** in one line before code: surface, audience,
   language, aesthetic family.
5. **Existing screens:** audit layer by layer (type, colour, surfaces,
   states) and improve inside the current stack; list the functions each
   change touches so none disappears.
6. **Spend the spectacle on a few signature moments** tied to the domain: a
   model revealed layer by layer with live counters, a quote that assembles,
   a thumbnail that becomes the detail view, a fluid scrubber, a command
   palette. Order them cheap identity first, differentiators second.
7. **Motion rules:** micro feedback 100-150 ms; menus and toasts 150-250 ms;
   panels 250-350 ms; ease-out entrances, shorter accelerated exits;
   `transform` and `opacity` only; every animation interruptible; no
   animation for keyboard and very frequent actions; a reduced-motion
   alternative for each (crossfade instead of travel); spinner after
   150-300 ms and shown at least 300 ms. Prefer CSS, then the Web Animations
   API, then a JS library; same-document View Transitions now work in all
   three engines.
8. **Verify once, in a batch:** Vitruvius `render_preview` at 390, 1024 and
   1440; `design_critique`; `page_assay` with focus, hover,
   `reduced_motion`, dark scheme and no-overflow criteria; the design
   detector over the changed files; the performance budget. Fix everything in
   one batch, confirm with at most one more round, stop.

## Pitfalls

- AI tells: purple-blue gradients, cards inside cards, one font for
  everything, three equal feature cards, glass everywhere, decorative loops.
- Smooth-scroll libraries in an app with nested scrolling panels and tables.
- Animating table sorting, tab switches or command palette opening.
- Subsetting or converting fonts whose licence forbids modification.
- Sending client screens or data to a hosted inspiration or critique service.
- Treating a critique score as proof of quality.

## Verification

- `DESIGN.md` lints without contrast errors and every colour in the UI comes
  from a token.
- Each surface has its dials written down and the signature moments listed
  with cost.
- `page_assay` passes for every changed page at all three widths, including
  reduced motion and keyboard focus.
- The design detector reports no new findings on changed files.
- Core Web Vitals stay inside the budget (LCP 2.5 s, INP 200 ms, CLS 0.1).
