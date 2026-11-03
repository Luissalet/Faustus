---
name: gsap-scrolltrigger
description: Build scroll-linked animation with GSAP ScrollTrigger — start/end, scrub, toggleActions, pinning, batch, fake horizontal scroll with containerAnimation, refresh and cleanup. Use when making scroll animations, parallax, pinned sections, scroll-telling pages or debugging triggers that fire at the wrong place.
version: 1.0.0
category: engineering
tags: [gsap, scrolltrigger, scroll, parallax, pin, scrub, animation, web, javascript, scrollytelling, desplazamiento]
status: published
source: imported
---

## When to Use

The page should react to scrolling: elements revealing as they enter, a section pinned while its content animates, a progress bar tied to scroll, horizontal panels driven by vertical scroll, or a trigger that fires too early or too late. Tweens and timelines themselves are in `gsap-core` and `gsap-timeline`; React cleanup in `gsap-react`; ScrollSmoother and scroll-to in `gsap-plugins`.

Adapted from greensock/gsap-skills (MIT, pinned aed9cfd32777); see THIRD_PARTY_NOTICES.md.

## Procedure

1. Register once at app level: `gsap.registerPlugin(ScrollTrigger)`.
2. Attach a trigger to a top-level tween or timeline with `scrollTrigger: { trigger, start, end, ... }`. `start`/`end` read "trigger-position viewport-position": `"top center"`, `"top 80%"`, `"bottom top"`, or relative `"+=300"`, `"+=100%"`. Defaults: start `"top bottom"` (`"top top"` when pinned), end `"bottom top"`.
3. Choose ONE behaviour: `scrub: true` (or a number of seconds of catch-up, e.g. `1`) to tie progress to scroll, or `toggleActions: "play reverse play reverse"` (onEnter, onLeave, onEnterBack, onLeaveBack) for discrete playback.
4. Pin with `pin: true` (or a wrapper element) plus an `end` such as `"+=1000"`. Animate children of the pinned element, not the pinned element itself. Keep `pinSpacing: true` unless layout handles the space.
5. Drive multi-step scroll stories with one timeline that owns the trigger: `gsap.timeline({ scrollTrigger: { trigger: ".story", start: "top top", end: "+=2000", scrub: 1, pin: true } })`.
6. Reveal many similar items with `ScrollTrigger.batch(".card", { onEnter: (els) => gsap.to(els, { autoAlpha: 1, y: 0, stagger: 0.1 }) })`. Batched callbacks receive (elements, triggers); do not pass `trigger`, `scrub`, `snap` or `toggleActions` to batch.
7. For fake horizontal scroll: pin the section, tween the inner track's `x`/`xPercent` with `ease: "none"`, and give nested triggers `containerAnimation: thatTween` with horizontal starts like `"left center"`.
8. Use `snap` (`0.25`, an array, `"labels"` or `{ snapTo, duration, ease }`) for stepped stories; standalone triggers without an animation use `ScrollTrigger.create({ ..., onUpdate: self => ... })`.
9. Turn on `markers: true` while building, then remove it.
10. After content that changes layout loads (images, fonts, async data) call `ScrollTrigger.refresh()`. Create triggers top to bottom in page order, or give each a `refreshPriority` matching page order.
11. On route change or unmount, kill or revert: `ctx.revert()` / useGSAP cleanup, or `ScrollTrigger.getById(id)?.kill()`.
12. When a third-party smooth scroller owns scrolling, set it up with `ScrollTrigger.scrollerProxy()` and register `ScrollTrigger.update` as its scroll listener.

## Pitfalls

- A trigger on a child tween inside a timeline: put it on the timeline.
- `scrub` and `toggleActions` on the same trigger: scrub wins and the actions are ignored.
- Any ease other than `"none"` on the containerAnimation tween breaks the scroll-to-position mapping; pinning and snapping are not available on containerAnimation triggers.
- Triggers created in random or async order without `refreshPriority` compute wrong positions once pin spacing is added above them.
- Positions measured before images or web fonts load: every start is off by the late content height.
- Leaving `markers: true` in production.
- Animating the pinned element itself, which fights the pin transform.

## Verification

- With markers on, scroll through and confirm each start/end marker sits where the design says; then remove markers.
- Take screenshots at several scroll positions (start, middle, end of each pinned section) and compare the frames with the storyboard.
- Resize the window and reload with a slow network: triggers must still fire at the same visual place.
- In a single-page app, navigate away and back and check `ScrollTrigger.getAll().length` stays constant.

## Reference

```javascript
gsap.registerPlugin(ScrollTrigger);
const track = document.querySelector(".track");
const scroll = gsap.to(track, {
  x: () => -(track.scrollWidth - window.innerWidth), ease: "none",
  scrollTrigger: { trigger: ".panels", pin: true, scrub: 1, end: () => "+=" + track.scrollWidth, invalidateOnRefresh: true },
});
gsap.from(".panel-2 h2", { y: 60, autoAlpha: 0,
  scrollTrigger: { trigger: ".panel-2", containerAnimation: scroll, start: "left center" } });
```
