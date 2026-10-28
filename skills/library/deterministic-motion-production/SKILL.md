---
name: deterministic-motion-production
description: Author seekable motion graphics with one timeline for preview and export. Use when producing animated titles, charts or overlays; source-linked narrated videos use source-explainer-production.
version: 1.2.0
category: engineering
tags: [motion, html, video, keyframes, composicion, animacion, estilo, tratamiento, hitos]
status: published
source: imported
---

## When to Use

Creating a motion composition from editable text, data and media. Establish
duration, frame rate, canvas and content; retain source and local assets.

## Procedure

1. Define duration, frame rate, dimensions, safe text bounds and audio
   timing. Prepare a shot plan identifying the information visible during
   each interval, rather than beginning with decorative transitions.
2. Discover the native composition and render tools of Prospero, which now
   incorporates both Generation and Montage. Do not launch a retired
   Lumiere service or an upstream renderer/CLI. Use local fonts and
   media and one authoritative timeline. Express animation as a function
   of time so seeking does not depend on earlier playback.
3. Build scenes with editable text, data and keyframes. Avoid random state
   or wall-clock effects unless the seed and their relation to timeline
   time are explicit. Wait for fonts and media before capturing frames.
4. Seek to several instants in shuffled order. Compare each result with
   the same instant reached through ordinary playback. Check transitions,
   media decode, text wrapping and the final frame.
5. Render locally, then inspect synchronization and image content at scene
   boundaries. Produce requested aspect ratios from the same content model
   and preserve an editable composition beside the delivered video.
6. Apply [the local scenario](references/scenario.json) when testing seek
   behavior. Its order deliberately revisits one timestamp after others;
   the second visit should produce the same visible composition.

### Direction flow when the film must look like a known style

Use it when the request names a look, or when the film will be judged by how it
feels. It needs the incorporated Montage tools of Prospero; discover them first
and stop at the step that is missing from the installed build.

7. Pick a style card with `motion_styles` (`search` by words or category, `get`
   with a `section`). Read its essence, materials, colour, type and motion
   sections. The card says what is safe to change, what changes the character
   and what is its essence: keep the essence, vary the rest. Keep the card's
   licence and attribution with the film. For a variant of a card use
   `studio_style_cards` with `from_card` and `changes`; do not edit the essence
   unless the user asks for another style.
8. Write the treatment before any HTML with `motion_treatment`: three different
   structures and why one is chosen, the shots one by one with why each is
   there, and the cue map (what the picture does at each moment and what is
   heard). `action=check` must pass. `motion_from_style` gives a scaffold with
   the card's palette; rewrite its page from the treatment, because the
   scaffold is a start, not the film.
9. Put the moments the picture and the sound hang on in `motion_cues`. The
   sound events are derived from the cues; do not edit events by hand while
   cues exist. Informative videos also keep `FACTS.md` with `motion_facts`:
   each claim with its source.
10. Check with the real page: `motion_cuecheck` (each cue has its visual hit in
    its frame, every visual hit has a cue; `retime` moves only the cues that
    are slightly off), `motion_readcheck` (every text stays fully visible for
    letters/15 s plus 1.5 s; a page that draws text in a canvas defines
    `window.TEXTS(t)`), and `motion_facts` `check` (numbers shown are in
    FACTS.md). Review the look with `html_motion_storyboard` in `strip` mode for
    the rhythm and `crop-at` for typography and small motion, not only a sheet.
    Then export with `motion_export` so the sound is part of the file.

## Pitfalls

- `source-explainer-production` handles supplied sources, scene narration,
  local voice and resumable MP4/SRT production. This procedure concerns the
  underlying deterministic motion composition; it does not replace that
  source-linked workflow or install another production harness.
- Smooth playback can still fail when frames render out of order.
- Missing fonts or late media loads alter line wrapping and frame content.
- A correct frame count does not establish the intended scene is present.
- A card is a vocabulary, not a guarantee: a scaffold that passes the checks can
  still be a poor film. The sync check measures frame changes, so a soft hit
  spread over many frames is reported as unclear; make the hit a single sharp
  change at the cue.
- Do not write the HTML before the treatment, and do not describe a card's
  look from memory: read its sections.

## Verification

- Repeated seeks to a time show the same composition.
- Preview and exported frames agree at tested timestamps.
- Local resources resolve and audio starts at its specified time.
- Text remains legible at every delivered canvas size.
- With a style card: `motion_cuecheck` reports every cue `ok` and no visual hit
  without a cue, `motion_readcheck` reports no text too short or never readable,
  the treatment passes its check, and the exported MP4 carries the sound.

## Provenance

Production reference: https://github.com/heygen-com/hyperframes at
7e00f37be2c76b4a9294c81aed79cf49667709f1 (Apache-2.0).
Original procedure for native local tools; no hosted renderer required.
The direction flow uses the direction style cards imported into Prospero (MIT;
the licence and attribution travel with each card; the demo films they mention
are not copied).
