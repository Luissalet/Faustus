---
name: gsap-plugins
description: Use GSAP plugins correctly — registration, Flip layout transitions, Draggable with Inertia, Observer, SplitText, ScrambleText, ScrollTo and ScrollSmoother, DrawSVG, MorphSVG, MotionPath and CustomEase. Use when the animation needs a GSAP plugin (text splitting, SVG drawing or morphing, drag, layout change, smooth scroll) or a plugin call fails.
version: 1.0.0
category: engineering
tags: [gsap, plugins, flip, draggable, splittext, morphsvg, drawsvg, motionpath, svg, text-animation, animation, web]
status: published
source: imported
---

## When to Use

The effect is beyond a plain tween: text that animates per line/word/character, an SVG stroke that draws itself, one shape morphing into another, an element following a path, a layout change (grid to list, card to modal) that should animate, drag with momentum, swipe/wheel gestures, smooth scrolling or scroll-to. Core API in `gsap-core`; scroll triggers in `gsap-scrolltrigger`.

Adapted from greensock/gsap-skills (MIT, pinned aed9cfd32777); see THIRD_PARTY_NOTICES.md.

## Procedure

1. Every plugin ships in the public `gsap` npm package: `import { SplitText } from "gsap/SplitText"`. Do not create private registry tokens or `.npmrc` entries, and do not tell users they need a paid membership.
2. Register each plugin once before first use: `gsap.registerPlugin(Flip, SplitText, DrawSVGPlugin)`. In React register at module level, never inside a re-rendering component.
3. Flip (layout changes): `const state = Flip.getState(".item")`, change the DOM or classes, then `Flip.from(state, { duration: 0.6, ease: "power2.inOut", absolute: true, nested: true })`. Options: `absolute` for flex/grid reflow, `scale` (default true), `simple` for speed, `onEnter`/`onLeave` for added/removed elements.
4. Draggable: `Draggable.create(".knob", { type: "rotation" | "x" | "y" | "x,y", bounds: ".track", inertia: true, edgeResistance: 0.65, onDragEnd })`. `inertia: true` needs InertiaPlugin registered.
5. Observer for gestures without scrolling: `Observer.create({ target: window, type: "wheel,touch,pointer", onUp, onDown, tolerance: 10 })` — the base of full-screen section-by-section experiences.
6. SplitText: `SplitText.create(".headline", { type: "words,lines", mask: "lines", autoSplit: true, onSplit: (self) => gsap.from(self.lines, { yPercent: 100, stagger: 0.08 }) })`. Split only what you animate; with `autoSplit` create AND return the animation inside `onSplit` so re-splits after font load or resize stay in sync. Keep `aria: "auto"` so screen readers read the original text. Call `revert()` on teardown.
7. ScrambleText / TextPlugin for decoding or typing effects on short strings only.
8. ScrollToPlugin: `gsap.to(window, { scrollTo: { y: "#pricing", offsetY: 80 }, duration: 1 })`. ScrollSmoother needs the `#smooth-wrapper > #smooth-content` structure and ScrollTrigger registered.
9. DrawSVG: the path needs a visible `stroke` and `stroke-width`; `gsap.from(path, { drawSVG: 0 })` draws in, `drawSVG: "20% 80%"` shows a segment. Fill is unaffected.
10. MorphSVG: `gsap.to("#circle", { morphSVG: { shape: "#star", type: "rotational", shapeIndex: "auto" } })`. Convert primitives first with `MorphSVGPlugin.convertToPath("circle, rect")`; use `shapeIndex: "log"` once to find a mapping that does not twist.
11. MotionPath: `motionPath: { path: "#route", align: "#route", alignOrigin: [0.5, 0.5], autoRotate: true }`.
12. Eases beyond the built-ins: `CustomEase.create("brand", ".17,.67,.83,.67")`, CustomWiggle and CustomBounce for shakes and bounces.
13. GSDevTools is a development scrubber only; remove it before shipping.

## Pitfalls

- Using a plugin property (`drawSVG`, `morphSVG`, `scrollTo`) without registering the plugin: it is silently treated as an unknown property.
- SplitText run before web fonts load: line breaks are wrong. Use `autoSplit: true` or `document.fonts.ready.then(...)`.
- Splitting characters only, which allows mid-word line breaks; split words too or set `smartWrap: true`.
- Morphing between shapes with different segment counts without `shapeIndex`/`map` adjustments produces a crossing, inside-out morph.
- DrawSVG on a shape with only a fill, or on content inside `<use>`, shows nothing.
- Leaving split text or Draggable instances alive after unmount.

## Verification

- Screenshot the start, middle and end of each plugin effect and inspect them; for text, check the reverted DOM equals the original text.
- Read the console for registration warnings.
- For Flip, toggle the layout back and forth several times quickly; elements must land in their final positions with no stretch.
- For SplitText with `autoSplit`, resize the window mid-animation and confirm lines re-split and the animation still targets them.

## Reference

```javascript
import { gsap } from "gsap";
import { Flip } from "gsap/Flip";
import { SplitText } from "gsap/SplitText";
gsap.registerPlugin(Flip, SplitText);

const state = Flip.getState(".tile");
document.querySelector(".grid").classList.toggle("list");
Flip.from(state, { duration: 0.6, ease: "power2.inOut", absolute: true });

SplitText.create("h1", { type: "words,lines", mask: "lines", autoSplit: true,
  onSplit: (s) => gsap.from(s.lines, { yPercent: 100, duration: 0.7, stagger: 0.06 }) });
```
