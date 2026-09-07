---
version: 1
slug: "studio-src-screens-studio-composer-tsx"
primary_target: "studio/src/screens/studio/Composer.tsx"
related_targets: ["studio/src/lib/clipboard-attachments.ts","studio/src/lib/attachment-uploads.ts","studio/src/adapters/composer.ts","studio/src/screens/studio.css"]
---

# Composer: paste screenshots as attachments

Local Operate extension of `studio/src/screens/studio/Composer.tsx`, preserving
DESIGN.md, its existing composition, upload endpoint and attachment semantics.

THESIS: Win+Shift+S then Ctrl+V in the message input immediately shows the image
as an attachment, with removal, upload feedback and recoverable failure.
OWN-WORLD: existing composer and attachment chips; no new palette or identity.
STORY: paste, see the local preview, type the request, send once upload is ready.
FIRST VIEWPORT: the screenshot appears above the input without a modal.
FORM: narrow behavior extension; inherited tokens, no concept/seed required.
FINISH: regression tests plus desktop/mobile synthetic browser checks and an
independent review completed on 2026-09-07. Verdict: SHIP, with no material
findings in the scoped composer implementation.

Preserve ordinary text paste and combined text/file content. Do not read the
clipboard in the background or fetch image URLs from pasted HTML. Reuse the
existing authorized upload route. Pending uploads must be cancellable and must
not attach to a different session after navigation; local object URLs must be
released. Enter and the Send button must both respect pending uploads.

## Implemented surface

- Immediate image preview above the input (64 × 64 px, `contain`), preserving
  the existing attachment strip and composer composition. Sent-message preview
  rules are unchanged.
- Shared paste/drop/file-picker queue: two concurrent uploads, a 60-second abort
  deadline per attempt, and queued/uploading/failed text feedback.
- Failed items retain their preview with retry and remove controls. Failure text
  wraps and uses the incumbent danger token. Attachment actions have at least
  44 × 44 px hit areas at widths up to 899 px.
- Native ordinary-text paste; selection-aware insertion for mixed text/files.
  No background clipboard access or fetching image URLs from pasted HTML.
- Existing upload endpoint and attachment semantics; cancellation on removal or
  session disposal, stale-completion protection and local object-URL cleanup.
- Enter, form submission and Send remain blocked by queued, uploading or failed
  entries until every local entry succeeds or is removed.

## Finish evidence and boundaries

The synthetic browser checks used the real Composer and Studio CSS with
controlled upload responses and virtual clipboard PNG data pasted with Ctrl+V.
Required captures are `.impeccable/review/composer-pending-desktop.png` at
1920 px and `.impeccable/review/composer-error-mobile.png` at 390 px.
Clipboard-parser and queue unit checks passed; TypeScript and 75 focused pytest
checks passed before unrelated provider work continued. The independent
reviewer returned SHIP with no material findings in this scope.

There is no total-files cap; the queue bounds concurrent uploads only. Physical
Win+Shift+S capture, the real upload service and model vision interpretation
were not verified. No production upload or LLM request was made in the browser
checks, and no commit or push was performed for this finish.

Detailed implementation and verification notes live in
`docs/ui/composer-clipboard.md`. These are surface-local facts, not a new product
identity. `DESIGN.md` and `.impeccable/design.json` are unchanged by this work.
