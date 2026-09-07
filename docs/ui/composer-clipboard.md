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
independent review completed; reviewer verdict SHIP with no material findings
in this scope. See verification and limits below.

Preserve ordinary text paste and combined text/file content. Do not read the
clipboard in the background or fetch image URLs from pasted HTML. Reuse the
existing authorized upload route. Pending uploads must be cancellable and must
not attach to a different session after navigation; local object URLs must be
released. Enter and the Send button must both respect pending uploads.

## Implemented behavior

- The message input consumes files from the user-initiated paste event. It
  prefers clipboard file items and falls back to the file list, avoiding two
  attachments for those two representations of the same transfer. Missing file
  MIME types use the item's MIME type when available.
- Ordinary text-only paste remains native. When files and plain text arrive
  together, text replaces the current selection and the caret is restored after
  it. Pasted HTML is not used to fetch remote images; there is no background
  clipboard read.
- Paste, drop and file selection feed the same session-local upload queue.
  Image files immediately receive a local object-URL preview above the input;
  non-image files retain the existing file icon treatment.
- At most two uploads run concurrently. Each upload attempt has a 60-second
  abort deadline. Queued, uploading and failed entries have explicit text
  feedback; failed entries retain their preview and offer retry or removal.
  Empty successful server responses also become recoverable failures.
- Uploads reuse `POST /api/upload`, same-origin credentials and the current
  session ID. Successful responses become existing attachment objects; previews
  then use the established `/api/upload/{id}` route. The adapter accepts an abort
  signal and excludes response entries without a nonempty string ID.
- The Send button, Enter handler and form submission all block while any local
  entry remains, including a failed entry. Retry successfully or remove the
  entry before sending. This prevents sending a request without its intended
  image while an upload is incomplete.
- Removal aborts an active request. Session changes and unmount dispose of the
  queue, cancel requests and ignore stale completions. Local object URLs are
  released on success, removal or disposal.

## Scoped visual contract

The existing composer slab, attachment strip, typography and semantic tokens
remain the visual authority. No product-wide identity or token changes were
needed; `DESIGN.md` and `.impeccable/design.json` remain unchanged by this work.

Composer image previews use an uncropped square area (64 × 64 px, `contain`).
This rule is scoped to the composer and does not change sent-message previews.
The attachment strip wraps. Error text wraps within a wider failed-attachment
layout, with the existing danger token on the border and message. A short,
muted hint makes screenshot paste and file drop discoverable.

Retry and remove keep accessible names that include the filename. Upload
feedback uses status semantics, and failures use alert semantics. At widths up
to 899 px, attachment action hit areas are at least 44 × 44 px, and composer
attachments are constrained to the available width.

## Verification and finish

Scoped finish recorded on 2026-09-07:

- Clipboard-parser and upload-queue unit checks passed.
- TypeScript checking and 75 focused pytest checks passed before separate,
  unrelated provider work continued. This is a scoped checkpoint, not a claim
  that the later whole worktree passed every check.
- Desktop and mobile synthetic browser checks exercised the real Composer
  component and Studio CSS with controlled upload responses. Browser virtual
  clipboard PNG data was pasted with Ctrl+V.
- Required captures: `.impeccable/review/composer-pending-desktop.png`
  (1920 px viewport width) and `.impeccable/review/composer-error-mobile.png`
  (390 px viewport width).
- Independent review returned **SHIP**, with no material findings in the
  reviewed composer scope. No commit or push was performed for this finish.

## Limits and unverified paths

The concurrency limit bounds active requests, not the total number of queued
files: there is no total-files cap. Existing server-side upload limits still
apply, but this pass did not verify the real upload service.

The browser exercise was synthetic, not a physical Windows Win+Shift+S capture.
No production upload or LLM request was made. End-to-end physical snip-to-paste,
real-service persistence and model vision interpretation remain unverified.

## Implementation references

- `studio/src/screens/studio/Composer.tsx`: input events, previews, recovery
  controls, session lifetime and send guards.
- `studio/src/lib/clipboard-attachments.ts`: file extraction and mixed-text
  insertion.
- `studio/src/lib/attachment-uploads.ts`: queue, cancellation, retry and cleanup.
- `studio/src/adapters/composer.ts`: existing upload transport and attachment
  response normalization.
- `studio/src/screens/studio.css`: composer-scoped preview, error layout and
  mobile attachment hit areas.
