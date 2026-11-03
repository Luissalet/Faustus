---
name: gsap-utils
description: Use gsap.utils helpers — clamp, mapRange, normalize, interpolate, random, snap, wrap, distribute, toArray, selector, pipe, getUnit, splitColor — including their reusable function form. Use when mapping scroll or pointer values into animation values, distributing values across many elements, or writing math by hand that gsap.utils already provides.
version: 1.0.0
category: engineering
tags: [gsap, utils, mapRange, clamp, distribute, random, snap, animation, javascript, interpolation, web]
status: published
source: imported
---

## When to Use

Converting one range into another (scroll progress into rotation, pointer x into hue), clamping or snapping values, spreading values across a grid of elements, producing deterministic random values in tween vars, or cleaning up hand-written helper math in an animation file. Works with `gsap-core`, `gsap-timeline` and `gsap-scrolltrigger`; no registration needed.

Adapted from greensock/gsap-skills (MIT, pinned aed9cfd32777); see THIRD_PARTY_NOTICES.md.

## Procedure

1. Prefer the function form when the same configuration is applied many times: omit the final value argument and the helper returns a function (`const toDeg = gsap.utils.mapRange(0, 1, 0, 360); toDeg(self.progress)`).
2. Ranges: `clamp(min, max, v)`, `mapRange(inMin, inMax, outMin, outMax, v)`, `normalize(min, max, v)` (to 0–1), `interpolate(start, end, progress)` — interpolate also blends colors, strings with numbers and objects.
3. Wrapping: `wrap(min, max, v)` loops a value (carousels, infinite indices), `wrapYoyo(min, max, v)` bounces it; `wrap(array)` cycles through array items.
4. Snapping: `snap(10, v)` to an increment, `snap([0, 100, 250], v)` to the nearest value, `snap({ values: [...], radius: 20 }, v)` only within a radius.
5. Random: `random(-100, 100, 5)` or `random(["red", "blue"])`; inside tween vars use the string form `"random(-100, 100)"` so each target gets its own value; pass `true` as the last argument to get a reusable generator.
6. Distribute values across many targets: `distribute({ base: 0.5, amount: 1, from: "center", grid: "auto", ease: "power1.inOut" })` returns a function GSAP calls per target; use it for scale, opacity, delay or x in grid effects.
7. Collections: `toArray(".item", scopeEl)` turns selectors and NodeLists into arrays; `selector(rootRef)` returns a scoped query function for components; `shuffle(array)` randomises in place.
8. Units and colors: `getUnit("40px")`, `unitize(fn, "px")` to add units to a function's output, `splitColor("#f60", true)` for HSL arrays.
9. Compose transforms with `pipe(clamp(0, 100), snap(5), unitize(..., "%"))` when a value passes through several steps.

## Pitfalls

- `mapRange` and `normalize` work on numbers, not unit strings; strip or add units with `getUnit`/`unitize`.
- Calling the helper with all arguments inside a hot handler re-parses the configuration each time; create the function form once.
- `Math.random()` in tween vars gives the same value to all targets when evaluated once; use `"random(...)"` strings or function-based values.
- `distribute` with both `amount` and `each`: pick one (amount splits a total, each sets a fixed step).
- Relying on undocumented helper behaviour; stick to the documented API.

## Verification

- Log the helper output for the edge inputs (min, max, beyond range) in the console before wiring it into a tween.
- For distribute/grid effects, screenshot the grid at the first frame and check the pattern starts from the intended origin.
- Grep the changed file for hand-written clamp/lerp helpers that can now be removed.

## Reference

```javascript
const progressToHue = gsap.utils.pipe(gsap.utils.clamp(0, 1), gsap.utils.mapRange(0, 1, 200, 340), Math.round);
ScrollTrigger.create({ trigger: ".page", start: "top top", end: "bottom bottom",
  onUpdate: (self) => document.body.style.setProperty("--hue", progressToHue(self.progress)) });

gsap.to(".dot", { scale: gsap.utils.distribute({ base: 0.4, amount: 1.2, grid: "auto", from: "center" }),
  rotation: "random(-30, 30, 5)", duration: 0.8 });
```
