---
name: gsap-performance
description: Keep GSAP animations smooth — transforms and opacity only, quickTo for pointer followers, stagger instead of many tweens, careful will-change, pinning and ScrollTrigger refresh, cleanup of off-screen work. Use when a GSAP or web animation stutters, drops frames, or must hold 60 fps on weak devices.
version: 1.0.0
category: engineering
tags: [gsap, performance, animation, jank, fps, compositor, web, javascript, rendimiento, fluidez]
status: published
source: imported
---

## When to Use

An animation janks, the fan spins on a landing page, a mouse follower lags, scroll animations stutter on a phone, or a review asks whether motion is cheap enough. Build the animation with `gsap-core` and `gsap-timeline`; scroll specifics are in `gsap-scrolltrigger`.

Adapted from greensock/gsap-skills (MIT, pinned aed9cfd32777); see THIRD_PARTY_NOTICES.md.

## Procedure

1. Measure before changing anything: record a Performance profile in the browser devtools while the animation runs and note long frames, layout and paint costs. Without a measurement, say so instead of guessing.
2. Replace layout properties with compositor properties: `x`/`y` instead of `left`/`top`, `scale` instead of `width`/`height`, `autoAlpha`/`opacity` for fades. `margin`, `padding`, `width`, `height`, `top`, `left` trigger layout every frame.
3. Add `will-change: transform` in CSS only to elements that actually animate, and remove it from elements that animate once and stay still. Do not set it, or `force3D`, on everything.
4. Replace loops of identical tweens with one tween and `stagger`. For long lists animate only what is visible (ScrollTrigger.batch or virtualization) instead of hundreds of simultaneous tweens.
5. For values that update constantly (mouse followers, drag readouts) create the setter once with `gsap.quickTo(target, "x", { duration: 0.4, ease: "power3" })` and call it in the handler; never create a new tween per `mousemove`.
6. Batch DOM reads before writes when mixing GSAP with manual layout code; interleaving `getBoundingClientRect()` with style writes forces repeated layout.
7. ScrollTrigger: pin only what must be pinned, prefer a numeric `scrub: 1` over `true` on heavy scenes, and call `ScrollTrigger.refresh()` only after real layout changes (content loaded, fonts ready), debounced. Viewport resize is already handled.
8. Pause or kill animations that are off screen or belong to a closed view; revert contexts on unmount so stray tweens and triggers stop.
9. Reuse timelines (restart/reverse) instead of creating new ones per interaction or per frame.
10. Simplify heavy SVG (fewer path points) and large blurred or shadowed layers before blaming the animation library.

## Pitfalls

- Optimising without a profile: the cost is often an image decode, a huge box-shadow or a filter, not the tween.
- `will-change` on many elements promotes too many layers and increases memory, which can make things slower.
- Leaving dev markers, GSDevTools or debug `onUpdate` logging in production.
- Calling `ScrollTrigger.refresh()` inside a resize or scroll handler on every event.
- Creating ScrollTriggers per item for a long list instead of `ScrollTrigger.batch()`.
- Forgetting cleanup in single-page apps: animations keep running on detached nodes.

## Verification

- Re-record the performance profile after the change and compare frame times and layout/paint counts with the first one; report both numbers.
- Test with CPU throttling (4x or 6x) in devtools as a stand-in for a weak device.
- Grep the changed code for `left:`, `top:`, `width:`, `height:` inside tween vars and for `will-change` on broad selectors.
- Navigate away and back in a single-page app and confirm `ScrollTrigger.getAll().length` does not grow.

## Reference

```javascript
const xTo = gsap.quickTo(".pointer-dot", "x", { duration: 0.4, ease: "power3" });
const yTo = gsap.quickTo(".pointer-dot", "y", { duration: 0.4, ease: "power3" });
window.addEventListener("pointermove", (e) => { xTo(e.clientX); yTo(e.clientY); });
```
