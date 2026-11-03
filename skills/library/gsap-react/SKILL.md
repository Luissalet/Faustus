---
name: gsap-react
description: Use GSAP inside React and Next.js safely — the useGSAP hook from @gsap/react, scoped selectors with refs, contextSafe event handlers, gsap.context with revert in useEffect, and client-only execution under SSR. Use when adding or reviewing animation in a React component, or fixing leaks and double animations after re-renders.
version: 1.0.0
category: engineering
tags: [gsap, react, nextjs, usegsap, animation, hooks, cleanup, ssr, frontend, javascript, animacion]
status: published
source: imported
---

## When to Use

A React (or Next.js, Remix, Vite React) component needs animation, an existing GSAP setup in `useEffect` leaks or animates twice under StrictMode, or event-handler animations survive unmount. Tween and timeline APIs are in `gsap-core` and `gsap-timeline`; scroll in `gsap-scrolltrigger`; Vue/Svelte in `gsap-frameworks`.

Adapted from greensock/gsap-skills (MIT, pinned aed9cfd32777); see THIRD_PARTY_NOTICES.md.

## Procedure

1. Check `package.json`; install what is missing with `npm install gsap @gsap/react`.
2. Register once at module level (outside components): `gsap.registerPlugin(useGSAP, ScrollTrigger)`.
3. Give the component a root ref and run all setup in `useGSAP(() => { ... }, { scope: rootRef })`. Selectors such as `".box"` are then searched only inside that root, and every tween and ScrollTrigger created during the callback is reverted automatically on unmount.
4. Pass dependencies when the animation depends on props or state: `useGSAP(fn, { dependencies: [value], scope: rootRef, revertOnUpdate: true })`. Without dependencies it runs once after mount.
5. Wrap animations created later (click, hover, pointer handlers, timers) with `contextSafe`: `const { contextSafe } = useGSAP({ scope: rootRef }); const onClick = contextSafe(() => gsap.to(".panel", { x: 100 }));`. Remove listeners you add manually in the cleanup function returned from the hook.
6. Target nodes through refs or scoped selectors, never through global selectors that can match other component instances.
7. Without @gsap/react, use `useLayoutEffect`/`useEffect` with `const ctx = gsap.context(() => { ... }, rootRef); return () => ctx.revert();` — always return the revert.
8. Keep a reusable timeline in a ref (`tl.current = gsap.timeline({ paused: true })`) when interactions play or reverse it, instead of recreating it on each render.
9. In Next.js App Router put animated components in a `"use client"` file; never call gsap or ScrollTrigger during server render. Dynamic `import("gsap/SplitText")` inside the hook is fine for rarely used plugins.
10. Respect reduced motion with `gsap.matchMedia()` inside the hook (it is reverted with the context).

## Pitfalls

- Unscoped selectors in a component that renders more than once animate every instance.
- Skipping cleanup: StrictMode mounts twice in development, so un-reverted setups animate twice and leave inline styles behind.
- Animations created in event handlers without `contextSafe` are not reverted on unmount and warn about unmounted nodes.
- Registering plugins inside a component body that re-renders.
- Reading refs before mount (top level of the component body) instead of inside the hook.
- Mixing React state updates per animation frame (`onUpdate: () => setState(...)`) — it re-renders 60 times a second; write to a ref or to the DOM instead.

## Verification

- Run the dev server (`process_start` for `npm run dev`), open the component, unmount and remount it (route away and back) and confirm no console warnings and no duplicated motion.
- In StrictMode check the animation plays once.
- Check `ScrollTrigger.getAll().length` before and after navigation stays equal.
- Build for production (`npm run build`) to catch SSR errors from gsap calls during server render.

## Reference

```jsx
"use client";
import { useRef } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
gsap.registerPlugin(useGSAP);

export function Cards() {
  const root = useRef(null);
  const { contextSafe } = useGSAP(() => {
    gsap.from(".card", { y: 24, autoAlpha: 0, stagger: 0.08 });
  }, { scope: root });
  const pulse = contextSafe((e) => gsap.fromTo(e.currentTarget, { scale: 0.97 }, { scale: 1, duration: 0.2 }));
  return <div ref={root}>{[1, 2, 3].map((n) => <button key={n} className="card" onClick={pulse}>{n}</button>)}</div>;
}
```
