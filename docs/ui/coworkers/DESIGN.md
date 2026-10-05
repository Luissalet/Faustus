---
name: Faustus Studio — Coworkers extension
description: Scoped record of the inherited specialist picker and persistent coworker editor.
colors:
  focus: "var(--fs-focus)"
  surface-panel: "var(--fs-surface-1)"
  surface-control: "var(--fs-surface-2)"
  text-primary: "var(--fs-text-1)"
  text-secondary: "var(--fs-text-2)"
  border: "var(--fs-border)"
rounded:
  control: "var(--fs-radius-control)"
  panel: "var(--fs-radius-panel)"
spacing:
  label: "var(--fs-space-1)"
  control: "var(--fs-space-2)"
  form: "var(--fs-space-3)"
  panel: "var(--fs-space-4)"
components:
  button-neutral:
    backgroundColor: "{colors.surface-control}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.control}"
    padding: "{spacing.control}"
  input-search:
    backgroundColor: "{colors.surface-control}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.control}"
    padding: "{spacing.control}"
  select-hoard:
    backgroundColor: "{colors.surface-control}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.control}"
    padding: "{spacing.control}"
  panel-coworkers:
    backgroundColor: "{colors.surface-panel}"
    rounded: "{rounded.panel}"
    padding: "{spacing.panel}"
---

# Design System: Faustus Studio — Coworkers extension

## Overview

**Creative North Star: "Faustus Studio's inherited personal workspace"**

This record covers the specialist filters and coworker editor only. The
[root DESIGN.md](../../../DESIGN.md) remains authoritative for Studio's
editorial, warm, technical identity. This extension introduces no palette,
typographic scale, shadow system, or new product identity. README.md and
README.es.md provide incumbent product context; no PRODUCT.md is introduced.

Ground truth is `studio/src/screens/agents/Coworkers.tsx`, `coworkers.css`,
the existing Studio foundations, and `config/agents/agency/specialists.json`.
The local [sidecar](.impeccable/design.json) records this surface, not a
replacement for a project-wide design registry.

**Key Characteristics:**
- Inherited, theme-aware Studio surfaces and readable neutral controls.
- Search and Hoard filtering before choosing a specialty.
- Visible responsibility and owner-maintained context when editing.

## Colors

The surface binds directly to the existing Studio custom properties. Warm
charcoal and warm paper themes remain owned by the root system; no fixed
theme snapshot is promoted into a new global token source.

### Primary

- **Studio focus:** the inherited keyboard-focus role, with a two-pixel
  outline and a three-pixel offset. It adds no accent alias or global token.

### Neutral

- **Panel surface:** encloses the coworker area at the first surface level.
- **Control surface:** distinguishes editable fields and clickable choices.
- **Primary and secondary text:** separate names and inputs from explanatory
  prose and the available-specialty count.
- **Border:** supplies the existing quiet contour for panel and controls.

**The Inherited Theme Rule.** Keep the surface bound to Studio custom
properties so theme and appearance density continue to apply.

## Typography

Controls use `font: inherit`; the surface adds no display face, local type
scale, or uppercase label system. The mounted Studio foundation supplies its
UI family, body size, and line height. Existing h2/h3 semantics distinguish
the coworker area from the specialty picker without adding a promotional
headline. Explanatory prose is bounded to 80ch.

## Layout

The panel uses the existing panel-spacing token for padding and block margin.
Its header and choice list are wrapping flex rows; choices remain ordinary
buttons. The filter row is a two-column grid: search gets two shares, Hoard
gets one. At 600px and below it stacks into a single column. Fields use
zero-minimum grid sizing and a maximum inline width of 100%.

The editor remains inline, with one vertical field sequence. Label gaps,
form gaps, and field padding reuse the existing spacing tokens, including
Studio density overrides. Main controls keep a minimum block size of 44px.

**The Bounded Choice Rule.** Narrow the specialty list in place and keep the
available count and no-match explanation next to the filters.

## Elevation & Depth

The coworker panel uses tonal layering and a one-pixel border. This surface
adds no shadow, floating card layer, blur, or motion grammar.

## Shapes

Panel and control corners reuse the existing Studio panel and control
radius tokens. No new radius scale or specialty-specific silhouette is
introduced. Flexible wrapping keeps choices usable at narrow widths.

## Components

### Buttons

Neutral, text-led controls identify both saved coworkers and available
templates. They share the control surface, border, inherited font and
spacing. Busy actions disable while saving, with explicit saving copy.
Keyboard focus uses the inherited focus token and remains visible on both
specialty filters and the template-choice controls.

### Inputs / Fields

Search and Hoard selection use visible labels and native input/select
semantics. Name, responsibility, context, Hoards and state remain editable
in the existing form. Responsibility and context use vertically resizable
textareas. Save errors remain visible in an alert.

### Specialty picker

Search matches name, responsibility and Hoard identifiers; the Hoard filter
combines with search. Choosing a result opens a draft based on its template.
The shipped catalog contains 50 templates, including the 38 added Hoard
profiles. These counts describe this release, not a permanent visual rule.

### Saved context

The editor's owner-maintained context field exposes the persisted context
without presenting unsaved or unverified notes as established memory.
Existing assignments and receipts stay attached to the coworker.

## Do's and Don'ts

### Do:
- **Do** reuse inherited Studio surfaces, typography and density tokens.
- **Do** keep visible labels, the available count and the no-match explanation.
- **Do** retain wrapping choice rows and stacked mobile filters.
- **Do** retain responsibility and owner-maintained context in the editor.

### Don't:
- **Don't** turn specialty categories into a new visual identity.
- **Don't** treat QA fixture names, counts or notes as production data.
- **Don't** promote this local composition into a global Studio layout rule.

<!-- Verification: D:/LocalAI/_hoard_research_20261004/qa-crm-specialists/report.json;
     specialists-1440.png and specialists-390.png. Actual shipped component
     exercised against an API fixture. Search, Hoard filtering, save and
     keyboard focus on both filters passed at 1440px and 390px;
     no browser errors. Fixture evidence does not establish live production data. -->
