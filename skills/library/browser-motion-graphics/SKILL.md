---
name: browser-motion-graphics
description: Build cinematic motion graphics in the browser (product promo, opener, intro, bumper, logo sting, kinetic typography, lower third, explainer in 16:9 or 9:16) with HTML, CSS and GSAP that move like video, not like slides — a style brief first, deterministic timeline time, anti-slideshow checks and frame-by-frame verification. Use when the user wants a motion graphic, animated promo, opener, kinetic type or explainer video made in HTML/GSAP.
version: 1.0.0
category: engineering
tags: [motion-graphics, gsap, animation, video, promo, opener, kinetic-typography, explainer, html, web, bang-motion, animacion, intro]
status: published
source: imported
---

## When to Use

A single self-contained `index.html` that plays like a video: promo or opener (25–40 s), bumper or logo sting (3–8 s), channel intro/outro (5–12 s), kinetic typography (15–60 s), lower thirds and titles (2–6 s), or a sourced explainer (30–90 s). For narrated, source-linked videos rendered through Prospero use `source-explainer-production`; for seekable compositions inside Prospero use `deterministic-motion-production`. GSAP API details are in `gsap-core`, `gsap-timeline` and `gsap-plugins`.

Adapted from bangtutorial/bang-motion (MIT, pinned c1aa65e921c9); translated and condensed, starters and scripts not bundled. See THIRD_PARTY_NOTICES.md.

## Procedure

1. Identify the kind (table above) and its duration. The deliverable needs only a modern browser: one HTML file, GSAP from a CDN or the project's package, no build step to watch it.
2. Absorb references and brand sources: logo, app icon, site, packaging, screenshots or a reference video. From a reference take rhythm (shot length, beat points), never its skin.
3. Write the style brief and show it with the rundown before any code: the product and three feeling adjectives; three candidate concepts with the chosen one and why, as `concept · number of scenes · main object per scene · opening · closing`; the palette with hex values and where each comes from (brand colour, background, one accent; derived colours say why); a display face with character plus a companion (avoid Poppins, Inter, Roboto, Montserrat, Arial unless they are the product's own UI font); the background motion language; the signature move.
4. Write the rundown as a table `second | scene | what moves | text | audio cue`. Two different products must differ in skin AND structure from your previous project.
5. Scaffold: one master `gsap.timeline({ paused: true })`, a camera rig (a world container you move, scale and rotate), scenes as functions that add child timelines at labels. Autoplay on open, loop at the end (`onComplete → tl.play(0)`), hidden keys (R restart, Space pause); a scrub panel only with `?debug=1`, autoplay held with `?clean=1` for export.
6. Determinism: every visual is a pure function of `tl.time()` — no `Date.now()`, no `Math.random()` per frame (use seeded or sine-based values), distances tweened rather than velocities accumulated, so scrubbing and frame export give identical frames.
7. Build scene by scene. Opener rule: one short sentence and at most one main object per scene, no kickers, sub-titles, body paragraphs or feature chips. New information gets a new visual response; the camera moves to follow or reveal something, and "zoom" means a change of shot size, not a barely visible breathing scale.
8. Text: sentence case, weight 500–600, no trailing period on short phrases; consecutive scenes differ in size and entry direction; text pockets sit on calm, dark-enough background.
9. Audio sync: find sentence boundaries with `ffmpeg -i vo.wav -af silencedetect=n=-35dB:d=0.3 -f null -` and place labels at them.
10. Export when asked: capture frames by seeking the timeline (Playwright, one PNG per frame at the target fps) and assemble with FFmpeg (`ffmpeg -framerate 30 -i f_%05d.png -c:v libx264 -pix_fmt yuv420p out.mp4`); see `ffmpeg-video-editing`. Screen recording drops frames — say so if that is the only option.

## Pitfalls

Slideshow checks — fail one and rebuild before showing the user:
- Three or more `<section>` scenes switched with opacity/autoAlpha: scenes must change because the world or camera moves (whip, push-through, cut to an instrument), not because a section fades.
- No visual through-line (one subject in continuous action, or one world the camera travels).
- Three text levels in a scene (kicker + title + body); two at most.
- Ken Burns as the only motion; every second needs motion from the chosen background language.
- Every transition is fade + scale bump + light leak.
- A miniature world: the camera so far out that houses are thumb-sized and empty ground fills half the frame.
- Template skeleton: zoomed text exiting left, three feature tiles in a row, a typed search box, hook → feature → feature → promise → logo → CTA.
- Starting from the dark-blue-glow look of a previous project; background is not dark by default.

## Verification

- Open the file, then seek the timeline to each label in shuffled order and screenshot each instant; the same instant reached by normal playback must look identical.
- Inspect the screenshots with `inspect_image` against the slideshow checks above and the brief's palette and fonts.
- Grep the source for `Date.now`, `performance.now` and `Math.random` outside seeded helpers.
- Check total `tl.duration()` against the brief, and that the loop restarts cleanly with audio.
- For an exported MP4, probe it (`inspect_media`) and look at a contact sheet of frames.
