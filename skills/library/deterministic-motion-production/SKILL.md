---
name: deterministic-motion-production
description: Author seekable motion graphics with one timeline for preview and export. Use when producing animated titles, charts, overlays or an explanatory video.
version: 1.0.0
category: engineering
tags: [motion, html, video, keyframes, composicion, animacion]
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
2. Discover the native composition and render tools. Use local fonts and
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

## Pitfalls

- Smooth playback can still fail when frames render out of order.
- Missing fonts or late media loads alter line wrapping and frame content.
- A correct frame count does not establish the intended scene is present.

## Verification

- Repeated seeks to a time show the same composition.
- Preview and exported frames agree at tested timestamps.
- Local resources resolve and audio starts at its specified time.
- Text remains legible at every delivered canvas size.

## Provenance

Production reference: https://github.com/heygen-com/hyperframes at
7e00f37be2c76b4a9294c81aed79cf49667709f1 (Apache-2.0).
Original procedure for native local tools; no hosted renderer required.
