---
name: modern-web-guidance
description: Look up the current browser-platform pattern in the vendored Chrome-team guides before writing web UI, then apply it with the fallbacks it names. Use when building or fixing a web interface in HTML, CSS or client-side JavaScript - modal dialogs, popovers, tooltips, tabs, responsive tables, dark mode, animations, forms and validation, accessibility, page speed, passkeys.
version: 1.0.0
category: engineering
tags: [web, frontend, html, css, javascript, dialog, modal, popover, tooltip, dropdown, tabs, carousel, accordion, responsive, dark, form, forms, autofill, validation, animation, transition, scroll, anchor, passkey, passkeys, accessibility, a11y, lcp, inp, cls, diálogo, formulario, animación, accesibilidad, interfaz, third-party]
status: published
source: imported
---

## When to Use

Interfaces web: diálogos modales, popovers, tooltips, tablas responsive, modo oscuro, animaciones y
transiciones, formularios con validación y autofill, accesibilidad, rendimiento de la página (LCP, INP, CLS) y
claves de acceso. Any task that writes or edits web UI: markup, styles or client-side script. The platform gained native answers
to many things that used to need a library or a hack (`<dialog>` and `popover`, invoker commands, anchor
positioning, `@starting-style`, view transitions, scroll-driven animation, `:user-invalid`, `field-sizing`,
passkeys), and a model's weights hold the older patterns. Run it first, before the first line of UI code, not
after a draft exists.

Skip it for backend code (SQL, ORMs, API routes), scripts and infrastructure, build tooling (bundler, tsconfig,
linter), tests, copy edits and renames, and third-party library documentation. For a whole-page visual
direction use `hallmark-page-design` or `product-ui-visual-direction`; this skill is about how the platform
does a thing, not how it should look. The guides are the Chrome team's modern-web-guidance set (Apache-2.0,
see `skills/guidance/modern-web-guidance/LICENSE`), vendored with a local index.

## Procedure

1. **Name the capability, one at a time.** Turn the request into a 2-5 word English phrase for one platform
   capability: a verb plus the UI object, optionally the feature name (`light dismiss dialog`, `responsive data
   table`, `anchored tooltip`). Leave out framework names, product names and file names. Translate intent into
   the primitive: "confirm before deleting" is a `confirmation dialog`, "pick a date" is a `date input`. A task
   with several capabilities gets one search each.
2. **Search.** Call `web_guidance` with `action=search` and the phrase (the MCP tools `web_guidance_search` and
   `web_guidance_retrieve` do the same for an agent outside the chat). Judge the hits by their one-line
   descriptions. If nothing fits, rephrase once with the concrete noun (`dialog`, `popover`, `table`, `form`),
   then `action=list` for one category. If still nothing fits, go on without a guide and say so.
3. **Retrieve the guide and its discipline guide.** `action=retrieve ids="light-dismiss-a-dialog,dialog"`. Read
   the specific guide, and the broad one for its area when it applies: `html`, `css`, `forms`,
   `accessibility`, `performance`, `security`. A long guide comes back in slices; continue with `offset` until
   `truncated` is false instead of working from the first slice.
4. **Apply it as written.** The guides assume features that are Baseline widely available are safe without
   fallbacks. For anything newer follow the fallback the guide gives, unless the project states its own support
   policy (a Baseline year, named browsers, "no polyfills"); then use the guide's support data to decide. When a
   stated policy excludes a guide's approach, take the simpler fallback, not a library.
5. **Adapt to the framework without dropping the feature.** The guides are vanilla HTML, CSS and JS. In a
   framework: pass newly shipped attributes through (spread them or cast the props) instead of deleting them;
   guard `window` and `HTMLElement` for server rendering; call `showModal()` and similar through refs on nodes
   the framework does not own; generate per-instance ids and `anchor-name`s; keep `:root` and `@starting-style`
   at top level.
6. **Cross-check before finishing.** Compare the code with each retrieved guide: the pattern, the fallbacks, the
   accessibility notes. Confirm the request is still fully met and no unrequested feature crept in.
7. **Say what was used.** End the reply with a short "Modern web guidance" note: one bullet per guide that
   shaped the code, in your own words - the non-obvious choice, override or fallback it required and the pitfall
   it avoids. If you searched and no guide applied, say which topics you checked and why none did.

Outside Faustus (an exported copy of this skill): if Faustus is connected as an MCP server, use its
`web_guidance_*` tools; otherwise find the id in `references/guide-index.md` and read the guide at the address
given there.

## Pitfalls

- Searching with a sentence about the product ("close the React settings modal on outside click") instead of the
  capability; the guides describe platform features, so framework and app nouns only add noise.
- One search for a compound task ("responsive table with dark mode"): split it, one capability per search.
- Reading only the search descriptions and writing from memory. The description is a pointer; the pattern and
  its fallbacks are in the guide.
- Taking the first slice of a long guide as the whole guide.
- Dropping a new attribute or API because the type definitions do not know it yet; cast it instead.
- Reaching for a library the guide shows is unnecessary, or removing a fallback the guide marks as required for
  a feature that is not yet widely available.
- Running the guides' legacy-pattern scan for a feature request. Modernising a whole codebase is a separate,
  explicitly requested job: grep the code for the old patterns the guides name, verify each hit is real, then
  retrieve the matching guide and replace them.
- Citing guide ids or quotes from memory after the context was trimmed. Search or retrieve again.

## Verification

- Every web capability in the task was searched, or the reply says a search found no guide.
- Each guide named in the closing note was actually retrieved in this turn, and the note describes a choice the
  guide required, not its introduction.
- Any feature that is not Baseline widely available has the guide's fallback in the code, or the project's
  stated support policy is cited as the reason it does not.
- The result still works without the framework's help: new attributes survive type checking and server
  rendering does not touch `window`.
- Accessibility parts of the guide (focus, labels, keyboard, reduced motion) are present in the code, not only
  the visual part.
