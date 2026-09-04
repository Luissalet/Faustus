# Component contracts — Faustus Studio primitives

Authority: `DESIGN.md` defines the tokens; this document defines the
primitives that consume them. A primitive is a React component built on
Radix UI that accepts data, returns JSX and exposes a stable `data-testid`.

Every component listed here must:

- Use only tokens from `DESIGN.md` (no hardcoded colors, radii, durations).
- Render the correct semantic HTML element (`<button>`, `<a>`, etc.).
- Have a visible focus ring (`--fs-focus`, never `outline: none`).
- Support `data-testid` for E2E tests.
- Work in dark mode, light mode, and `prefers-reduced-motion: reduce`.

---

## P0 — Primitives (UI-011)

### Button

```tsx
interface ButtonProps {
  variant: 'primary' | 'secondary' | 'ghost' | 'danger';
  size?: 'sm' | 'md' | 'lg';          // default: 'md'
  label: string;
  icon?: LucideIcon;                   // leading icon
  iconPosition?: 'left' | 'right';    // default: 'left'
  loading?: boolean;
  disabled?: boolean;
  onClick: () => void;
}
```

| Prop | Effect |
|------|--------|
| `variant="primary"` | `--fs-brand` background, `--fs-on-brand` text — CORRECTED: `--fs-text-1` on coral measures 2.85:1 and fails AA |
| `variant="secondary"` | `--fs-surface-2` background, `--fs-border`, `--fs-text-1` text |
| `variant="ghost"` | transparent background, `--fs-text-2` text |
| `variant="danger"` | OUTLINE: transparent background, `--fs-danger` border and text. See DESIGN.md colour rule 0 |
| `variant="danger-solid"` | `--fs-danger-solid` background, white text. Only the confirming button inside a destructive dialog |
| `loading` | replaces icon with spinner; disables interaction; `aria-busy` |
| `disabled` | `opacity: 0.4; pointer-events: none` |

**HTML:** `<button type="button">`. Never a `<div>`.
**Sizes:** sm = 28 px height, md = 36 px, lg = 44 px.
**Radius:** `--fs-radius-control`.
**Hover:** one surface step lighter, 120 ms.
**Active:** `scale(0.98)`, 120 ms.
**Focus:** `outline: 2px solid var(--fs-focus); outline-offset: 2px`.
**Test ID:** `data-testid="btn-{label-slug}"`.

### IconButton

```tsx
interface IconButtonProps {
  icon: LucideIcon;
  'aria-label': string;               // required — no icon-only button without label
  variant?: 'ghost' | 'secondary';    // default: 'ghost'
  size?: 'sm' | 'md';                 // default: 'md'
  badge?: number | boolean;           // notification dot or count
  disabled?: boolean;
  onClick: () => void;
}
```

**HTML:** `<button type="button" aria-label="...">`.
**Size:** sm = 28×28 px, md = 36×36 px. Touch area always ≥ 44×44 on mobile.
**Test ID:** `data-testid="icon-btn-{aria-label-slug}"`.

### StatusBadge

```tsx
interface StatusBadgeProps {
  status: 'queued' | 'running' | 'waiting' | 'paused' | 'succeeded' | 'failed' | 'cancelled';
  label?: string;                      // override the default text
  size?: 'sm' | 'md';                 // default: 'sm'
}
```

**Rendering:** icon + text. Color is secondary — the icon and label
convey meaning. Never rely on color alone.

| Status | Icon | Color |
|--------|------|-------|
| queued | `Clock` | `--fs-text-3` |
| running | `Loader2` (animated) | `--fs-info` |
| waiting | `AlertCircle` | `--fs-warning` |
| paused | `Pause` | `--fs-text-3` |
| succeeded | `CheckCircle` | `--fs-success` |
| failed | `XCircle` | `--fs-danger` |
| cancelled | `MinusCircle` | `--fs-text-3` |

**HTML:** `<span role="status">`. No `<div>`.
**Radius:** `--fs-radius-pill` (semantic pill).
**Animation:** `running` icon spins. In `prefers-reduced-motion`, it
pulses opacity instead.
**Test ID:** `data-testid="status-{status}"`.

### EmptyState

```tsx
interface EmptyStateProps {
  icon?: LucideIcon;                   // 24 px illustration icon
  title: string;
  description?: string;
  primaryAction?: { label: string; onClick: () => void };
  secondaryAction?: { label: string; onClick: () => void };
}
```

**Layout:** centered vertical stack. Icon → title (heading) →
description → action buttons.
**Title:** uses `<h2>` or the correct heading level in context.
**Max width:** 360 px for comfortable reading.
**Test ID:** `data-testid="empty-{title-slug}"`.

### Menu (via Radix DropdownMenu)

```tsx
interface MenuProps {
  trigger: React.ReactNode;
  items: MenuItem[];
  align?: 'start' | 'center' | 'end';
}

interface MenuItem {
  label: string;
  icon?: LucideIcon;
  shortcut?: string;
  disabled?: boolean;
  danger?: boolean;
  onSelect: () => void;
}
```

**Keyboard:** Arrow keys navigate, Enter/Space selects, Escape closes
and returns focus to the trigger.
**Focus:** first item on open; returns to trigger on close.
**Radius:** `--fs-radius-panel`.
**Background:** `--fs-surface-3` with `--fs-border-strong` 1 px border.
**Shadow:** overlay-level shadow.
**Test ID:** `data-testid="menu-{trigger-label-slug}"`.

### Dialog (via Radix Dialog)

```tsx
interface DialogProps {
  title: string;
  description?: string;
  children: React.ReactNode;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}
```

**HTML:** `aria-modal="true"`, focus trap via Radix, `inert` on content
behind the dialog.
**Close:** Escape key, close button (visible, top-right), click outside.
**Focus:** moves to first focusable element on open; returns to trigger
on close.
**Background:** dimmed overlay at `rgba(0,0,0,0.5)`.
**Radius:** `--fs-radius-panel`.
**Test ID:** `data-testid="dialog-{title-slug}"`.

### Popover (via Radix Popover)

```tsx
interface PopoverProps {
  trigger: React.ReactNode;
  children: React.ReactNode;
  align?: 'start' | 'center' | 'end';
  side?: 'top' | 'right' | 'bottom' | 'left';
}
```

**Keyboard:** Escape closes and returns focus.
**Collision:** auto-flips when near viewport edge (Radix built-in).
**Radius:** `--fs-radius-panel`.
**Test ID:** `data-testid="popover-{context}"`.

### Skeleton

```tsx
interface SkeletonProps {
  shape: 'text' | 'circle' | 'rect';
  width?: string;
  height?: string;
  lines?: number;                      // for shape="text"
}
```

**HTML:** `<div role="status" aria-busy="true" aria-label="Loading">`.
**Animation:** subtle shimmer using a CSS gradient animation.
In `prefers-reduced-motion`: static gray block, no animation.
**Color:** `--fs-surface-3` on `--fs-surface-1` background.
**Test ID:** `data-testid="skeleton"`.

---

## P1 — Product objects (UI-031+)

These build on P0 primitives. Listed here for contract stability; they
are implemented in later tickets.

### ArtifactCard

```tsx
interface ArtifactCardProps {
  preview?: string;                    // image URL or thumbnail
  type: 'image' | 'video' | 'document' | 'code' | 'data';
  title: string;
  project?: string;
  status: StatusBadgeProps['status'];
  date: string;                        // ISO date
  skill?: string;
  actions: MenuItem[];                 // context menu
  onSelect: () => void;
}
```

**Image dimensions:** explicit `width` and `height` on `<img>` to prevent
layout shift.
**Test ID:** `data-testid="artifact-{type}-{title-slug}"`.

### RunTimeline

```tsx
interface RunTimelineProps {
  steps: RunStep[];
  collapsed?: boolean;
}

interface RunStep {
  id: string;
  label: string;
  status: StatusBadgeProps['status'];
  detail?: string;
  duration?: number;                   // ms
  cost?: number;
}
```

**Test ID:** `data-testid="timeline-{step.id}"` per step.

### ApprovalCard

```tsx
interface ApprovalCardProps {
  action: string;                      // what the agent wants to do
  target: string;                      // file, service, destination
  risk: 'low' | 'medium' | 'high';
  payload?: Record<string, unknown>;   // data being sent
  onApprove: (scope: 'task' | 'session') => void;
  onDeny: () => void;
}
```

**Visual risk indicator:** left border color by risk level.
**Test ID:** `data-testid="approval-{action-slug}"`.

### ContextBar

```tsx
interface ContextBarProps {
  project?: { id: string; name: string };
  skill?: { id: string; name: string };
  references?: number;
  backend?: string;
  budget?: { estimated: number; unit: string };
  memory?: 'project' | 'global' | 'none';
  onChipClick: (type: string) => void;
  onChipRemove: (type: string) => void;
}
```

Each context piece is a removable chip. Clicking a chip opens its
editor (popover or inspector panel).
**Test ID:** `data-testid="context-{type}"` per chip.

---

## Cross-cutting rules

1. **`data-testid` is mandatory** from the first component. It is not
   added later. The naming convention is `{component}-{identifier}`.

2. **No `<div>` with `onClick`.** Every interactive element is a
   `<button>`, `<a>`, or a Radix primitive that renders the correct
   element.

3. **No color or radius outside tokens.** If a component needs a new
   value, add it to `DESIGN.md` first.

4. **No `transition: all`.** Specify the properties being transitioned.

5. **No `outline: none` without a replacement.** The `--fs-focus` ring
   is always visible on `:focus-visible`.

6. **CSS is per-component**, colocated in the `studio/` tree. Not in
   `static/style.css`.
