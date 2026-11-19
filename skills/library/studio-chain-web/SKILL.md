---
name: studio-chain-web
description: Run a web order from the request to brief, cited design direction, code, headless browser tests, rendered critique and assay, local service and a publication hand-off, with what each station passes on. Use when someone orders a web page or small web app, or when you direct the workers who build it.
version: 1.0.0
category: planning
tags: [studio, chain, pipeline, web, design, code, testing, critique, publication, handoff, cadena, web, diseno, codigo, pruebas, publicacion]
status: published
source: imported
---

## When to Use

An order whose deliverable is a page or a small web app (a shop window, a form, a gallery). Each station has its own skill; this one gives order, hand-offs and checks.

## Procedure

1. **Order.** Register it (Hub order tools when available, otherwise one Agora task per station).
2. **Brief** (`studio-drucker-engagement-brief` or `expediente.md` in the order folder): pages, content, what a visitor can do, what is stored, acceptance criteria that a test can run.
3. **Design direction** (`studio-vitruvius-design-critique`, first half): cited brief, accessibility rules, `direccion.md`.
4. **Pieces and code.** Images made for the order (rendered in `studio-blender-headless-render` or generated in Prospero) with their origin; HTML, CSS and scripts; a small server if something is stored. Check scripts with a syntax check before serving.
5. **Tests** (`studio-headless-web-testing`) on the served page, three environments, persistence and failure path.
6. **Render, critique and assay** (`studio-vitruvius-design-critique`, second half) on a self-contained HTML copy; fix and repeat with the tests until both pass.
7. **Local service** for review: start and stop scripts, PID, log, URL; a test database.
8. **Publication hand-off.** A reproducible ZIP that serves the same page when unpacked (check by unpacking, serving and submitting once). There is no deploy station yet: Mercator records posts and their metrics and its `post_publish` needs the URL of a page already published. Say that publication is pending a destination.

## Hand-offs

- Brief to design: the acceptance criteria become the assay criteria and the browser test steps.
- Design to code: `direccion.md` with each decision and its citation.
- Code to tests: URL, how to start and stop, where data is stored.
- Code to critique: a single self-contained HTML file (styles, scripts and images inlined).
- Everything to publication: the ZIP, its check, and the open question of the destination and of who answers the form's requests.

## Defects

A failed test or a floor finding goes back to the code station with width, colour scheme, state and the screenshot; a design decision that the critique keeps flagging goes back to the design station with the finding and its rule.

## Pitfalls

- Testing the portable HTML only: storage works only on the served site.
- Placeholder images presented as final.
- A local server left running without its PID and stop script written down.
- Calling a page published when it only runs on loopback.

## Verification

- Browser tests and assay both passing on the final version, screenshots looked at.
- The ZIP unpacked, served, one submission stored and read back.
- The report says what is not done: public hosting, handling of incoming requests.
