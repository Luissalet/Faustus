---
name: anime-ui-controls
description: Offer and wire in ready-made controls with a distinct style when building an app or website - anime reactions (Sakuga VFX), a 1930s rubber-hose cartoon look (Rubber Hose UI) or pixel-art scenes (Pixel Worlds) - choose the set and the control from the brief, preview it, install it into the project with its licence, keep the control's real keyboard and form behaviour, and make a new control in the same style from the set's rules. Use when someone asks for an anime, manga, cartoon, retro, pixel-art or game-like look for buttons, toggles, sliders, tabs, toasts or loading bars, or asks for a playful style for an app or website.
version: 1.0.0
category: engineering
tags: [ui, style, anime, manga, sakuga, cartoon, pixel-art, controls, buttons, animation, web-components, game, vitruvius, botones, estilo, interfaz, dibujos, juego]
status: published
source: imported
---

## When to Use

A brief where the look of the controls is a decision: a game or anime site, a playful tool, a portfolio, a launch page, a children's or retro product. Not for a work screen the person lives in all day; there the controls stay quiet (`product-ui-visual-direction`). The style sets are Vitruvius's craft library entries (`ui-set` and `ui-control`); this skill is how to offer them, install one and keep it accessible.

Adapted from the skill of github.com/finktheartist/sakuga-vfx (MIT, pinned at commit c3dabb173928a3ad2372183103caf2e6f50953d0); the sets bundled in Vitruvius are that project and github.com/finktheartist/rubber-hose-ui and github.com/finktheartist/pixel-worlds (MIT). See THIRD_PARTY_NOTICES.md.

## Procedure

1. Offer the options before building: when the brief names a look, say which set matches (anime: Sakuga VFX; 1930s cartoon: Rubber Hose UI; pixel art and fantasy: Pixel Worlds) and show the preview page; when the brief does not name one and the product allows it, offer one set as a choice and say what it costs (a canvas over the page, a few hundred KB of script). The person decides.
2. When Vitruvius's Hoard is connected (it starts on demand; if its tools are not offered say so and use step 8): `craft_search` with `kind ui-set` for the style and `kind ui-control` for the control, `craft_read` for what each control is for and when to avoid it, `craft_page` with `kind ui-control` and the id `set/control` for a complete preview page, and the reference pages under `/craft/ui-sets/`.
3. Choose control by control: use the set's control only where its reaction helps the person (the confirm button, a settings switch, a progress bar); keep native elements for dense data entry, long forms and anything people operate hundreds of times a day.
4. Install: `ui_set_install` with the set id, the project folder and the control ids copies the scripts and the licence into `<folder>/<set>/` and returns the script tags and markup. Load each with a plain `<script src>` tag, not as a module; do not rewrite or split the file. The first script on a page sets the shared core and the rest reuse it, so any mix of controls shares one canvas and one clock.
5. Wire it: state through attributes and properties, events for what happened (`change` for value controls, `click` for buttons, and each control's own done event for progress); the value controls work inside a `<form>` with a `name`. Colours are the set's CSS variables (`--sk-*` for Sakuga): set them in the page's `:root`, do not edit the effects to restyle.
6. Keep it accessible, which is part of the job: every control keeps a visible text name (a `label` attribute or its text), is reached with Tab and operated with its keys, shows a focus indicator (the catalogue marks controls whose look needs one added with CSS), has an accessible equivalent for anything only drawn (a done check, a toast message in a live region), and the effects switch off under `prefers-reduced-motion`. Read `accessible-interfaces` and run its check.
7. Verify by use, in a real browser at a desktop width and at 390 px: press and drag with real pointer events, Tab to the control and use its keys, submit the form and look at the form data, and read the reaction frame by frame when it matters (stop the clock, step to chosen moments, read each frame). "Stepped frame by frame" is not "watched at real speed"; the report says which was done and which beats still read weakly.
8. Without the Hoard: the sources are public (the repositories above, MIT). Fetch one control file with `web_fetch`, keep the licence file beside it, and follow steps 5 to 7. Nothing here needs an account.
9. A new control in the same style, only when the set has none: decide four things first (it still works with every effect off as a real `<button>`, `<input>` or `role="switch"`; the beats written as anticipation, hit, follow-through and settle with one or two effects each; at most one sound-effect word, on the hit; nothing the person needs to read depends on an effect), then build on the set's core.
10. For anime game effects as sprite sheets (fire, water, thunder, wind and the rest) use `ui_set_fetch` for the `kanaworks-sakuga-vfx` entry: it downloads one effect and writes its credit line beside it. The licence requires the credit `VFX: KanaWorks_AI (@KanaWorks_AI)` wherever the effects are used and forbids redistributing them as a pack; show the credit in the page footer or the credits screen and never copy the files into a shared library.

## Pitfalls

- Two tones and a line, nothing else: no gradients or blur in the drawn effects; things leave by shrinking or thinning, not by fading; squash keeps volume; springs, not easings, for anything the hand moves; effects follow speed; the control is plain at rest. Breaking these makes a control look like a different set.
- Idle animation, or effects around a control before it is used.
- Using the set for every control on a page: the effect that delights once tires on the tenth control.
- Loading the script as a module, splitting it, or changing the effects to restyle it.
- Claiming the look works because the page loaded without errors; a component that runs says nothing about how it reacts.
- Shipping the on-demand effects without their credit.

## Verification

- The preview and the installed page show the control; the console has no errors; the licence file is beside the scripts.
- Tab reaches each control, each shows a focus indicator, and the keys work; the form submits the control's value.
- Both widths were looked at (no sideways scroll at 390 px), and reduced motion leaves the state change working with no effect.
- The report separates what was seen at real speed from what was stepped, and names the weak beats.
