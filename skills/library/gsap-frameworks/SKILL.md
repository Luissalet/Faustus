---
name: gsap-frameworks
description: Use GSAP in Vue, Nuxt, Svelte, SvelteKit, Astro and other component frameworks — create after mount inside gsap.context scoped to the component root, revert on unmount, register plugins once, lazy-load rarely used plugins and stay SSR-safe. Use when adding animation to a non-React component framework or fixing animations that leak between routes.
version: 1.0.0
category: engineering
tags: [gsap, vue, nuxt, svelte, sveltekit, astro, animation, lifecycle, cleanup, ssr, frontend, web]
status: published
source: imported
---

## When to Use

Animation in a Vue 3 / Nuxt, Svelte / SvelteKit, Astro island or similar component: entrance animations, scroll triggers per page, hover timelines, or animations that keep running after navigation. React has its own hook: see `gsap-react`.

Adapted from greensock/gsap-skills (MIT, pinned aed9cfd32777); see THIRD_PARTY_NOTICES.md.

## Procedure

1. Three rules for every framework: create tweens and ScrollTriggers only after the component's DOM exists; scope selectors to the component root; revert everything on unmount.
2. Register plugins once at app level (Vue `main.js`, a Nuxt plugin or composable, a SvelteKit root `+layout.svelte` script), not in every component.
3. Vue 3: hold a template ref (`const root = ref(null)`), build in `onMounted(() => { ctx = gsap.context(() => { ... }, root.value) })` and call `ctx?.revert()` in `onUnmounted`. The same applies with `<script setup>`.
4. Nuxt: wrap GSAP access in a composable that registers common plugins and lazy-loads rare ones with dynamic imports (`await import("gsap/SplitText")`, then `gsap.registerPlugin(mod.SplitText)`). Keep gsap calls in `onMounted` or `<ClientOnly>` so nothing runs during server render.
5. Svelte: `bind:this={root}`, then in `onMount` create `const ctx = gsap.context(() => { ... }, root)` and `return () => ctx.revert()`. Svelte 5 runes follow the same mount/destroy principle (`$effect` with a cleanup return).
6. Astro: run GSAP in a client script or a hydrated island (`client:visible`), never in frontmatter.
7. Create ScrollTriggers inside the same context so the revert kills them; after async data renders (Vue `nextTick`, Svelte `tick`), call `ScrollTrigger.refresh()`.
8. For animations that react to props or state, revert and rebuild in a watcher, or keep one timeline and update it, rather than stacking new tweens.
9. Respect reduced motion with `gsap.matchMedia()` inside the context.

## Pitfalls

- Creating animations in setup code before mount: the refs are null and selectors find nothing.
- Global selectors without a context scope animate other instances or other pages.
- No revert on unmount: triggers from a previous route keep firing and pin spacing stays in the DOM.
- Registering plugins in a component body that runs for every instance.
- Calling gsap during SSR in Nuxt or SvelteKit crashes the server render or causes hydration mismatches.

## Verification

- Start the dev server with `process_start`, navigate into and out of the animated route several times, and confirm no console warnings and a constant `ScrollTrigger.getAll().length`.
- Render two instances of the component on one page and check only each one's own elements animate.
- Run the production build (`npm run build`) to catch server-render errors.

## Reference

```vue
<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import { gsap } from "gsap";
const root = ref(null);
let ctx;
onMounted(() => { ctx = gsap.context(() => { gsap.from(".item", { autoAlpha: 0, y: 16, stagger: 0.08 }); }, root.value); });
onUnmounted(() => ctx?.revert());
</script>
<template><ul ref="root"><li class="item" v-for="n in 3" :key="n">{{ n }}</li></ul></template>
```
