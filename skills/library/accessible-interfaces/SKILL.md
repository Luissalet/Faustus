---
name: accessible-interfaces
description: Make every page, app screen, artifact or component you generate usable from the keyboard and by a screen reader without being asked - native elements first, a name for every control, a visible focus indicator, logical Tab order, complete ARIA only where no native element exists, enough contrast, reduced motion, dialogs and menus that trap and return focus - then check it with Vitruvius's interaction rules and a real Tab walk when connected. Use when building or editing a web page, app screen, form, dialog, menu, tabs, dashboard or any interface, including landing pages and artifacts.
version: 1.0.0
category: engineering
tags: [accessibility, a11y, keyboard, focus, aria, screen-reader, contrast, forms, dialog, interface, html, css, frontend, accesibilidad, teclado, interfaz, pagina, formulario, web]
status: published
source: imported
---

## When to Use

Any time the output is something a person will operate: a page, a screen of an app, an artifact, a form, a dialog, a menu, tabs, a dashboard, a component. It applies without the person asking for accessibility; an interface that only works with a mouse is unfinished. It adds to the design and motion skills (`hallmark-page-design`, `product-ui-visual-direction`, `gsap-core`, `anime-ui-controls`), it does not replace them. When the project already uses `@ui-sdk/*` packages, follow `ui-sdk` for the widgets and keep this checklist for everything around them.

The rules follow the common baseline of the WAI-ARIA authoring practices; the checks are Vitruvius's `interaction` rule pack and its keyboard walk. The detailed WCAG 2.2 criteria are in `web-accessibility-design`, and `modern-web-guidance` (the `web_guidance` tool) has the guide for each native element and behaviour (dialog, popover, details, forms).

## Procedure

1. Start from native elements. Navigation is `<a href>`, an action is `<button type="button">`, a choice is `<input type=checkbox|radio>` or `<select>`, a section is `<details>`, a modal is `<dialog>` opened with `showModal()`. They bring the role, the keys, the focus and the form value for free. Reach for a `role` only when no native element exists, and then complete it (below).
2. Give every control a name a person can read or hear: visible text, a `<label for>` (or the label wrapping the field), `aria-label` for an icon-only button, `aria-labelledby` for a dialog or group. A placeholder is not a name. Decorative icons get `aria-hidden="true"`; informative images get `alt`.
3. Make the order and the focus right. Write the source in the order Tab should go; never use a tabindex above 0; `tabindex="-1"` only for what a script focuses. Every interactive element shows a visible `:focus-visible` indicator (a 2 to 3 px outline with offset, or a ring with 3:1 contrast); never remove the outline without a replacement. Offer "skip to content" when there is repeated navigation.
4. Complete any custom widget by its role: `dialog` needs a name and focus moved inside, kept inside while open, and returned to the trigger on close, with Escape closing it; `tablist` > `tab` with `aria-selected`, `aria-controls`, roving `tabindex` and arrow keys; `switch` and `checkbox` need `aria-checked`; `slider` needs `aria-valuenow`, `aria-valuemin`, `aria-valuemax` and the arrow, Home and End keys; `combobox` needs `aria-expanded` and `aria-controls`; menus and listboxes move with arrows and close on Escape. Space and Enter activate what looks like a button.
5. Keep a click handler on a control, not on a `div`. If a `div` must be clickable it gets the role, `tabindex="0"` and Enter and Space handlers; a link that runs code is a `<button>`.
6. Forms: one `<label>` per field, `autocomplete` on the common fields, errors linked with `aria-describedby` and announced (`role="alert"` or `aria-live`), required fields marked in text and not only in colour, a submit button that is a real submit.
7. Colour and motion: text 4.5:1 against its background (3:1 for large text), control edges, focus rings and state fills 3:1, information never by colour alone. Wrap non-essential motion in `@media (prefers-reduced-motion: no-preference)` or switch it off under `prefers-reduced-motion: reduce`; nothing flashes more than three times a second; nothing auto-scrolls without a pause control. Target size at least 24 px, 44 px for touch.
8. Set `<html lang>`, one `<h1>`, headings in order, landmarks (`header`, `nav`, `main`, `footer`), a `<title>`, and `meta viewport` without disabling zoom.
9. Check it before saying it is done. When Vitruvius's Hoard is connected, run `design_critique` (or `design_lint` for the static rules alone) on the page: the `interaction` rules report controls without a name, click handlers on non-controls, incomplete ARIA widgets, dangling references and duplicate ids, and the browser capture walks the page with Tab and reports controls only a pointer can use, tab stops with no visible focus indicator and controls Tab never reaches (`keyboard` in the result). Fix the Floor findings, then re-run. Without the Hoard, open the page in a headless browser, press Tab through it and read where focus goes, and check contrast with the computed colours.

## Pitfalls

- `outline: none` on `:focus` with nothing in its place. Use `:focus-visible` to keep the ring for the keyboard and drop it for the mouse.
- `aria-label` on a `div` with no role: it is ignored. ARIA that contradicts the native element (a `role="button"` on a link that navigates) is worse than none.
- Hidden content that stays focusable (`opacity: 0`, off-screen); use `hidden`, `inert` or `display: none`.
- A custom select, date picker or modal written from scratch when a native element or the project's widget library exists.
- Disabling the browser's zoom, trapping focus with no Escape, or an infinite carousel with no pause.
- Treating a clean automatic report as the whole job: tools do not judge whether the order makes sense or the names mean something. Read the page the way a keyboard user would.

## Verification

- Tab from the top of the page reaches every control, in reading order, each with a visible focus indicator; Shift+Tab goes back; Escape closes what opened and focus returns to its trigger.
- Every control has a name that makes sense out of context (not "click here", not "button").
- `design_critique` returns no open `interaction/` Floor findings and the `keyboard` summary shows no pointer-only control, or the check was done by hand and the report says which way.
- Contrast and reduced-motion behaviour were checked in a real browser, or the report says they were not.
