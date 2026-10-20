---
name: hoard-design-loop
description: Build or fix a web page with Vitruvius's Hoard in the loop — a cited brief first, then render, critique, fix, and a functional check before calling it done. Use when the user asks for a landing, a page, a component or "make this look less generic"; en español: «hazme/diséñame una landing», «una página web», «que no parezca genérica».
version: 1.1.0
category: engineering
tags: [hoards, vitruvius, frontend, design, critique, assay, tokens, landing, web, pagina, diseno, disename, maqueta, comprueba]
status: published
source: imported
---

## When to Use

Any HTML, CSS or front-end page the user will look at: a new landing, a redesign, "critica esta página", "¿funciona?". Not for pure backend work or copy-only edits.

## Procedure

Vitruvius now owns Design, API documentation/environments and incorporated
Inspection. Use its configured native owner and explicitly requested module;
tool discovery must not start an app. Daguerre owns free vector illustration,
and Prospero owns Generation and Montage. A request for an editable flowchart
belongs to `diagram-authoring`, rather than generating a web page around it.

1. **Brief before code.** For a page from scratch call `design_brief` with the
   subject, the vibe in the user's words and the product type (it matches
   Spanish words too). It returns cited styles, one palette, one font
   pairing, rules per area and the anti-patterns to avoid. If the user wants
   a reusable system, `tokens_generate` (name, base colour or vibe, `style`
   as a catalogue style name, `mode`) gives CSS variables to build on;
   `tokens_get` with `format: "css"` returns them ready to paste. Tools are
   `mcp__<connector>__<name>`; one `lookup_tools` call loads their schemas.
2. **Write the page** in the existing project's framework, or as a self-contained HTML document when that is the requested artifact: the brief's fonts
   and palette, a real `<meta name="viewport">`, `lang`, and copy about the
   user's subject (never lorem ipsum, never "Your Company").
3. **Look at it.** `render_preview` with the HTML (widths 390, 1024, 1440).
   It never returns images unless asked; read `lint.findings` and
   `metrics`. Then `design_critique` with the `render_id`: a 0-10 score and
   findings, each with a fix.
4. **Fix and render again.** Apply the fixes that carry `severity: error`
   first (contrast, viewport, missing alt, placeholder copy), then the
   warnings. Keep the existing bounded critique loop (up to three rounds)
   and report the observed score and unresolved findings. A score of eight
   is a useful signal, not proof that the page works or meets the brief.
5. **Check it works.** `page_assay` with the final `render_id` (or the HTML):
   it exercises supported controls in a real browser and reports contradictions —
   a button that does nothing, `undefined` on screen, a counter that goes
   the wrong way. Fix every failing case before handing the page over.
6. **Deliver**: the file (written in the workspace when there is one), the
   last score, what `page_assay` checked, and the `[vitruvius: …]` cites of
   the rules you followed.

## Output shape

```
landing.html — puntuación 8,5/10 tras 2 rondas (antes 3/10).
Arreglado: contraste del CTA 3,8:1 → 7,1:1, viewport, «Lorem ipsum», degradado violeta→azul.
Queda: líneas de 95 caracteres en el aviso legal (texto del cliente).
page_assay: 14 casos, 14 pasan (menú, formulario, acordeón).
Criterio: [vitruvius: impeccable/… § Color] [vitruvius: emil-skills/… § Easing]
```

## Pitfalls

- Writing first and asking for the brief afterwards: the brief is what keeps
  the page away from Inter on white with three cards.
- Treating the lint as the verdict: `page_assay` is what says the page works.
- Quoting the library without its cite, or paraphrasing a rule into
  something it does not say.
- Asking for screenshots (`include_images`) on every round; the findings
  carry the numbers. Inspect actual desktop/mobile images in a bounded
  final pass; do not infer visual quality from a lint score alone.
- Calling `reference_add`, `source_add` or `tokens_delete` without the
  user asking: they change the app's data.

## Verification

- A `render_preview` of the final HTML and its critique score are in this turn.
- `page_assay` ran on the final version and every failing case was fixed or named.
- Every rule quoted to the user carries its `[vitruvius: …]` cite.
