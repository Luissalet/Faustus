---
version: 1
slug: "studio-src-screens-studio-tsx"
primary_target: "studio/src/screens/Studio.tsx"
related_targets: ["studio/src/voice/VoicePanel.tsx","studio/src/voice/voice.css","studio/src/voice/VoiceOrb.tsx"]
---

# Studio voice extension

Mode: Operate. Existing Studio identity and controls remain authoritative.
This is an inline extension, not a new visual world or a replacement screen.

## Direction contract

THESIS: Speak to the existing assistant without losing the transcript or control of its work.
OWN-WORLD: Studio's warm surfaces, coral identity, blue listening state, inherited typography and controls.
STORY: Open voice, see its provider, speak, review the transcription, send, hear the answer, interrupt sound independently of work.
FIRST VIEWPORT: Inline two-column panel above the composer. A 230px sampled sphere left; status, elapsed time, transcript and controls right. Mobile stacks a smaller sphere above controls.
FORM: User-pinned reactive sphere, local extension; no concept seed required. Real audio RMS deforms the point field; idle rotation is presence, never fake audio. Reduced motion and hidden-tab suspension.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

No raster assets. Inherits DESIGN.md without changing its global identity.

## Implemented surface

This is the local Jarvis-style voice extension to the existing chat, not a
separate assistant. Root `DESIGN.md` remains the visual authority; no global
tokens, design-system sidecar, or product identity were changed by this
documentation pass. No `PRODUCT.md` was introduced.

- **Material and type:** warm Studio surface and border tokens, inherited UI
  font, coral primary action, blue listening/focus, danger error state.
  The panel is flat with a one-pixel border (16px corners); local controls
  use tighter corners (8px). These are implementation measurements, not
  additions to the global token scale.
- **Desktop composition:** centered inline panel above the composer, capped
  at 1000px wide and 62dvh high with internal scrolling. Its visual column
  spans 150–230px beside a flexible status/control column. Heading (21px,
  600), phase (17px, 500), compact metadata (12px), and editable transcript
  (14px) preserve a readable hierarchy.
- **Mobile composition:** at 640px and below, the panel stacks with a 128px
  visual, 16px padding and a 65dvh height cap. Voice buttons have a 44px
  minimum height. While voice is open, the composer uses a shrinkable
  single-column grid; secondary knobs scroll horizontally on their own
  row so Send and voice controls remain visible at the reviewed 390px width.
- **Integration:** `Studio.tsx` lazily mounts `VoicePanel` for the active
  voice session only. Its `data-voice` attribute scopes the flex-stage and
  composer adjustments. The chat remains scrollable; header and composer
  do not shrink.

## Reactive sphere

`VoiceOrb.tsx` draws an 850-point sampled sphere and orbit in canvas, using
the existing theme tokens. Input/output analyser RMS drives smoothed size
and field deformation; absent audio produces no simulated audio amplitude.
Idle rotation is the pinned local presence treatment, not a new global
motion rule. Normal drawing is throttled to about 30fps with pixel density
capped at 2. Reduced motion removes rotation and wave deformation and
throttles drawing to 5fps; RMS size feedback remains. Drawing skips hidden
tabs, and the panel pauses capture/playback when the tab is hidden.

The canvas is hidden from assistive technology. Visible microphone text,
a live phase status, elapsed seconds and inline error alerts carry meaning
independently of the visual. Focus-visible outlines cover interactive
controls. No generated or shipped raster assets are used; review PNGs are
evidence, not product imagery.

## Interaction and lifecycle

Voice shows input/output provider location and language choice before
capture. Transcription is reviewable and editable by default; automatic
sending and listening again are explicit options. Thinking exposes the
running tool when available, while sensitive approvals stay in the chat.
Silencing speech, muting the microphone, closing voice or changing the
active chat does not cancel the task; Cancel task is a separate action.

Muted replies still observe task completion, approvals and errors. Turning
off read-aloud aborts queued playback, clears the analyser, and leaves the
speaking phase for thinking or idle. Opening voice during existing work
observes status without reading old messages aloud. Alt+V starts/finishes
capture and Escape mutes.

## Finish status and evidence

The bounded finish review closed three findings: mobile composer overflow
hiding Send/voice, muted replies losing task-status updates, and read-aloud
disable leaving the speaking phase active. Desktop and 390px mobile
captures are retained at `.impeccable/review/desktop.png` and
`.impeccable/review/mobile.png`.

Review scope is intentionally limited: screenshots cover **idle only**;
the lifecycle corrections were **source-reviewed**, not demonstrated in
those captures. Physical microphone capture and a live LLM conversation
remain **unverified**. This completes local surface documentation, not a
broader accessibility, theme, backend or end-to-end certification.
