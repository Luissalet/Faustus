---
name: gsap-timeline
description: Sequence GSAP animations with gsap.timeline — position parameter, labels, defaults, nesting and playback control. Use when choreographing several steps in order or in parallel, building keyframe-like sequences, or replacing delay chains with a timeline.
version: 1.0.0
category: engineering
tags: [gsap, animation, timeline, sequencing, choreography, javascript, web, motion, secuencia, animacion]
status: published
source: imported
---

## When to Use

Any animation with more than one step: intros, reveal sequences, a hero that builds in parts, a loader, a product walkthrough, or a scroll-driven sequence (then also read `gsap-scrolltrigger`). Single tweens and eases live in `gsap-core`; React lifecycles in `gsap-react`.

Adapted from greensock/gsap-skills (MIT, pinned aed9cfd32777); see THIRD_PARTY_NOTICES.md.

## Procedure

1. Create one timeline per sequence: `const tl = gsap.timeline({ defaults: { duration: 0.5, ease: "power2.out" } })`. Children inherit `defaults`, so most child tweens only state what changes.
2. Append steps with `tl.to()`, `tl.from()`, `tl.fromTo()`. Without a position they run one after another.
3. Place steps with the position parameter (third argument): an absolute time `1`; relative to the end `"+=0.5"` or `"-=0.2"`; `"<"` to start with the previous step, `">"` to start when it ends, `"<0.2"` 0.2 s after the previous start; a label `"intro"` or `"intro+=0.3"`.
4. Add labels for every named beat: `tl.addLabel("reveal", "+=0.2")`. Labels make retiming and seeking (`tl.play("reveal")`, `tl.seek("reveal")`) readable.
5. Build complex pieces as functions that return a child timeline, then assemble them in a master: `master.add(intro(), 0).add(features(), "+=0.2")`. Each part stays testable and re-orderable.
6. Use constructor options for the whole sequence: `paused: true` (start with `.play()`), `repeat`, `yoyo`, `repeatDelay`, `onComplete`, `onUpdate`.
7. Control playback through the returned instance: `play()`, `pause()`, `reverse()`, `restart()`, `time(2)`, `progress(0.5)`, `timeScale(2)`, `kill()`. `tl.tweenFromTo("intro", "outro")` returns a tween that scrubs the playhead between two labels.
8. For a scroll-driven sequence, put `scrollTrigger` in the timeline constructor, never on a child tween.
9. Keep durations short for interface feedback (0.15–0.4 s) and longer for entrances (0.5–0.9 s); use one or two eases for the whole sequence so it reads as one piece.

## Pitfalls

- Chaining with `delay` instead of position parameters: moving one step means recomputing every later delay.
- A `duration` on the timeline constructor does not set child durations; the timeline's length comes from its children. Use `defaults`.
- A ScrollTrigger inside a child tween of a timeline: the timeline and the trigger fight over the playhead. ScrollTriggers belong on top-level timelines or tweens only.
- Several `from()` steps on the same property of one element need `immediateRender: false` on the later ones, or the earlier step is invisible.
- Building a new timeline on every click or render instead of reusing one and calling `restart()`/`reverse()` leaks animations and stacks conflicting tweens.
- Overlapping steps that animate the same property of the same element; the later one wins mid-way and the motion jumps.

## Verification

- Seek to each label (`tl.seek("label").pause()`) in the browser console and take a screenshot at each beat; compare against the intended storyboard.
- Run `tl.progress(1)` and check the final state is the designed one, then `tl.progress(0)` and check the initial state has no flash of the end state.
- Confirm total length with `tl.duration()` matches the brief.
- Reverse the timeline and check nothing is left half-visible (`autoAlpha` restored).

## Reference

```javascript
const tl = gsap.timeline({ defaults: { duration: 0.6, ease: "power3.out" }, paused: true });
tl.from(".title", { y: 40, autoAlpha: 0 })
  .addLabel("details", "+=0.1")
  .from(".lede", { y: 20, autoAlpha: 0 }, "details")
  .from(".cta", { scale: 0.9, autoAlpha: 0 }, "<0.15")
  .to(".halo", { rotation: 90, ease: "none", duration: 2 }, 0);
tl.play();
```
