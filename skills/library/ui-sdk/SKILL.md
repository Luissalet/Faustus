---
name: ui-sdk
description: Build interface behaviour with UI SDK, a library of keyboard navigation, focus, selection, dismissal, popup, drag-and-drop and ARIA behaviours with adapters for React, Vue, Svelte, Solid and web components - compose its ready components for forms, settings and command palettes, build a missing component from its core behaviours, make it usable by agents, and test keyboard, focus, ARIA, server output and right-to-left. Use when a project has @ui-sdk packages installed, or when an interface needs a dialog, menu, select, combobox, tabs, tree, slider or drag-and-drop that must work from the keyboard.
version: 1.0.0
category: engineering
tags: [ui, accessibility, keyboard, focus, aria, react, vue, svelte, solid, web-components, dialog, menu, combobox, select, interfaz, teclado, accesibilidad, components, third-party]
status: published
source: imported
---

## When to Use

An interface that has real widgets - popups, lists you choose from, menus, tabs, trees, sliders, reorderable lists - where each one has to behave correctly from the keyboard and with a screen reader. Use it when `@ui-sdk/*` is already in `package.json`, or when the work is a settings page, form, command palette or dashboard and you would otherwise hand-write `role="listbox"`, `role="menu"`, `role="dialog"` or `role="tablist"` markup. For the baseline every generated interface must meet (names, focus, keys, contrast, motion) read `accessible-interfaces`; for plain pages without a widget library the native elements in `accessible-interfaces` are enough.

Adapted from github.com/vercel-labs/ui (Apache-2.0, pinned at commit 10b3b6c0eccfd176a6d60ce4efaccf0d1584da08; skill `packages/core/skills/ui-sdk`, its Markdown references condensed); see THIRD_PARTY_NOTICES.md. The library's own docs ship inside each installed package and are the API; this skill is the working method.

## Procedure

1. Read the project first: framework, package manager, stylesheet entry, test setup, whether `@ui-sdk/core` and the framework package (`@ui-sdk/react`, `vue`, `svelte`, `solid` or `web`) are installed at the same version. Install what is missing with the project's own package manager; in a Nuxt 4 app add `"@ui-sdk/vue"` to `modules`. A page with no build step loads the library's `web.js` and `web.css` from its site instead (network needed) and reads its single-file web components doc.
2. Read the docs of the installed version, not memory: `node_modules/@ui-sdk/<framework>/docs/llms.txt` is the index; then read only the component pages you need (`docs/components/select.md`). Do not read compiled `dist/*.js`; the docs and the `.d.ts` types are the API and, when they do not answer, a small test that tries it does.
3. Pick the path. Path 1, build the interface from ready components: choose each one from `references/pick-a-component.md`. Path 2, a widget the library does not ship (stepper, splitter, transfer list, emoji picker): compose it from the core behaviours with `definePrimitive`, so it gets the same keyboard, focus, server rendering and right-to-left. Most interfaces are path 1; use path 2 only for the piece that is missing.
4. Apply the rules for every path:
   - Import each component from its own entry (`@ui-sdk/react/select`), never from a package root and never from `@ui-sdk/core/internal/*`.
   - One shape everywhere: a root and its parts nested as the component page's Composition shows. Change a part's element with `render`, not an `as` prop.
   - Start with `defaultValue`; control a value only when the app needs it, through `value` plus `onValueChange(value, details)` (or the framework's idiom: `v-model`, `bind:value`, element properties and events). `details` has `reason`, `source` and `cancel()`.
   - Name every control with its label part (`FieldLabel`, `DialogTitle`) or `aria-label`; people with screen readers and agents both find controls by name.
   - Style from state: target `data-slot` and state attributes (`data-open`, `data-pressed`), never inline styles for state and never generated ids. Put `@layer ui-sdk;` at the top of the global stylesheet, above any utility-framework import. With the optional default look (`web.css`), read the library's styles page first: it draws icons and sets sizes you cannot see in the markup.
   - Check colour in a real browser: 4.5:1 for text, 3:1 for control edges, focus rings and state fills; jsdom cannot see colour.
   - Key lists by value or id, never by index. Right-to-left needs no provider: components read the direction from the page.
   - Before writing any `role=` attribute look for the component. A `div` with `role` listbox, menu, dialog, tablist, separator, switch, slider, radiogroup, toolbar or tree next to an installed ui-sdk package is almost always a bug.
5. Forms: give a control a `name` and it submits with its form, selects included; wrap controls in `Field` with `FieldLabel`, `FieldDescription` and `FieldError`; `required`, `disabled` and `readOnly` work as the native ones; for a server error pass `invalid` to the Field.
6. Make it agent-ready, in addition to person-ready: every control has a unique accessible name where it sits, actions go through the library's parts (a bare `<button>` or `<input>` is invisible to the agent bridge, use `Button`, `Input`, `Select` inside a `Field`), state flows through change callbacks and not through key or click handlers, and a custom primitive ends with an `agent: { actions: [...] }` key. Connect the bridge once in browser code with `createAgent` from the same package (`agentTools` has the tool names and schemas to route to your own model; `registerTools` exposes it to a browser agent). Mark with `data-agent-confirm`, `data-agent-secret` and `data-agent-hidden` the regions that need a person, a secret or a hidden area.
7. Test what you built, not what the library already tests: that the right components are wired with names, values, callbacks and what submits; press Space on every switch and checkbox you placed; check that closing a dialog or menu returns focus where it was; and give every interface at least one agent test that snapshots the components and acts through them. For a new primitive use `@ui-sdk/core/testing` (`mount`, keys, type, click, drag, then `checkPattern`, `checkServer`, `checkDirection`). Anything with popups, layout or focus moving between fields also gets a run in a real browser.

## Pitfalls

- Reading the whole `docs/` folder: it is large. Read the index, then the pages you need; search `docs/core.md` (over 300 KB) by heading only for path 2.
- Calling the library from a server component without `"use client"` in your own file when it uses state, effects, event handlers or `createParts`. Svelte: never put `$state` at module level (shared across requests on the server). Solid: keep reads reactive, never copy a signal into a variable first. Web components: parts that keep native behaviour need the native element inside them.
- Money in a number input with the default step of 1 rounds the cents; use `step={0.01}`.
- `minLength` does not stop a value an agent sets; validate with `pattern` or your own check.
- Two controls with the same label in one row (an agent finds by label); give each a verb of its own.
- Comments that say what the code does. Write one only for a browser quirk or a trade-off, in one or two lines.

## Verification

- `npx tsc -b` (or `tsc --noEmit -p <app config>`; `svelte-check`, `vue-tsc`) passes. If the root `tsconfig.json` has `"files": []` and `references`, `tsc --noEmit` alone checks nothing.
- The test run exits 0 with the tests above present; each command is reported with its exit code.
- In a real browser, Tab reaches every control in a sensible order, each shows a visible focus indicator, and Escape closes what opened.
- Anything not checked (colour in a real browser, right-to-left, server output) is named as unchecked, and anything the docs or types got wrong or left out is reported.
