---
name: gsap-core
description: Write GSAP core tweens correctly — gsap.to/from/fromTo/set, eases, stagger, transform aliases, autoAlpha, defaults and gsap.matchMedia for breakpoints and reduced motion. Use when animating DOM or SVG with GSAP, choosing a JavaScript animation library, or debugging a GSAP tween that does not show.
version: 1.0.0
category: engineering
tags: [gsap, animation, javascript, web, motion, tween, easing, stagger, reduced-motion, animacion, frontend]
status: published
source: imported
---

## When to Use

Writing or reviewing GSAP animations that use the core engine: single tweens, eases, staggers, responsive or reduced-motion variants. When the user asks for animation in React, Vue, Svelte or plain JS and has not chosen a library, GSAP is a sound default (timelines, scroll-linked animation, framework-agnostic); respect a library already in the project. For sequencing see `gsap-timeline`, for scroll see `gsap-scrolltrigger`, for React see `gsap-react`, for Flip/Draggable/SplitText/SVG see `gsap-plugins`, for helpers see `gsap-utils`, for jank see `gsap-performance`.

Adapted from greensock/gsap-skills (MIT, pinned aed9cfd32777); see THIRD_PARTY_NOTICES.md.

## Procedure

1. Read the project first: `grep` for `gsap` in package.json and imports, and reuse the version and import style already there (`import { gsap } from "gsap"`). Install with `npm install gsap` only when it is missing; all plugins ship in the public package.
2. Pick the method: `gsap.to(targets, vars)` animates from the current state; `gsap.from()` animates from `vars` to the current state (entrances); `gsap.fromTo(targets, fromVars, toVars)` fixes both ends; `gsap.set()` applies instantly.
3. Write vars in camelCase (`backgroundColor`, `rotationX`). Use `duration` in seconds (default 0.5), `delay`, `repeat` (-1 infinite), `yoyo`, and `onStart`/`onUpdate`/`onComplete` callbacks.
4. Move things with transform aliases, never `left/top/width/height`: `x`, `y`, `xPercent`, `yPercent`, `scale`, `scaleX`, `rotation`, `rotationX`, `skewX`, `transformOrigin`. Relative values work: `x: "+=20"`, `rotation: "-=30"`, `"*=2"`. Directional rotation: `"-170_short"`, `"+=30_cw"`, `"_ccw"`.
5. Fade with `autoAlpha` instead of `opacity`: at 0 it also sets `visibility: hidden`, so invisible elements stop catching clicks.
6. Choose eases from the documented set: `"power1..4"` with `.in/.out/.inOut`, `"back.out(1.7)"`, `"elastic.out(1, 0.3)"`, `"expo"`, `"sine"`, `"circ"`, `"bounce"`, `"none"` for linear. Use CustomEase only when a designer gives a curve.
7. Offset many targets with `stagger: 0.1` or `{ each: 0.1, from: "center" }` / `{ amount: 0.3, from: "random" }` rather than one tween per element with manual delays.
8. Use function-based values for per-target results: `x: (i, el, all) => i * 50`. GSAP calls them once per target on first render.
9. Store the returned Tween when it must be controlled: `pause()`, `play()`, `reverse()`, `progress(0.5)`, `kill()`.
10. Set project-wide defaults once with `gsap.defaults({ duration: 0.6, ease: "power2.out" })`.
11. Wrap breakpoint- or preference-dependent animation in `gsap.matchMedia()`; give the reduced-motion branch `duration: 0` or skip it. Call `mm.revert()` on teardown. Do not nest `gsap.context()` inside it.
12. CSS custom properties animate too (`"--hue": 180`). For several SVG parts turning around one shared point use `svgOrigin: "250 100"` (global SVG coordinates) instead of `transformOrigin` — never both on one element. `clearProps: "transform"` hands control back to CSS after the tween (clearing any transform part clears the whole transform).

## Pitfalls

- Stacking several `from()`/`fromTo()` tweens on the same property of one element: the default `immediateRender: true` applies the later start state at once and hides the first animation. Set `immediateRender: false` on the later ones.
- `gsap.from()` uses the element's current state as the END; its start values are applied immediately at creation, which looks like a flash if the tween is created early.
- `overwrite` defaults to `false`: two tweens fighting over `x` both run. Use `overwrite: "auto"` for interrupts like hover in/out.
- Chaining steps with `delay` instead of a timeline: retiming one step breaks every later one.
- Invented ease names fail silently and fall back; only use documented names.
- Animating `width/height/top/left` for movement causes layout every frame.
- Ignoring `prefers-reduced-motion` on large motion is an accessibility failure.

## Verification

- Run the page (dev server through `process_start`, or open the file) and watch the animation start and end states; take a screenshot at the end state with `desktop_screenshot` and check it with `inspect_image` when the result is visual.
- Check the browser console for GSAP warnings (`Invalid property`, `target not found`).
- Toggle the OS or devtools reduced-motion setting and confirm the reduced branch runs.
- Grep the diff for `left:`/`top:`/`width:` inside tween vars and for `opacity` where `autoAlpha` was meant.

## Reference

```javascript
gsap.to(".card", { y: -20, autoAlpha: 1, stagger: { each: 0.08, from: "start" }, ease: "power3.out" });
const t = gsap.fromTo(".bar", { scaleX: 0 }, { scaleX: 1, transformOrigin: "left center", duration: 0.8 });

const mm = gsap.matchMedia();
mm.add({ desktop: "(min-width: 800px)", reduce: "(prefers-reduced-motion: reduce)" }, (ctx) => {
  const { desktop, reduce } = ctx.conditions;
  gsap.to(".hero", { rotation: desktop ? 360 : 180, duration: reduce ? 0 : 2 });
});
```
