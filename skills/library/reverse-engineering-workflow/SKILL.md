---
name: reverse-engineering-workflow
description: Investigate local software and derive a reproducible behavior specification. Use when explaining a feature, recovering a format or comparing an independent reconstruction.
version: 1.0.0
category: research
tags: [reverse-engineering, binary, specification, investigar, ingenieria, inversa]
status: published
source: imported
---

## When to Use

Investigating a local application, executable, package or format. Begin with
one question about behavior and a reference input. Keep observations,
interpretations and unknowns distinct throughout the investigation.

## Procedure

1. Identify the exact target revision and local artifact. State the feature
   being investigated and which input and output will decide the question.
2. Discover the installed analysis tools. Inspect source, symbols, strings,
   calls, package boundaries or a recorded trace as appropriate. Use local
   free engines; no paid engine or hosted analysis API is required.
3. Follow data from its entry point to the visible effect. Retain the file,
   function or recorded interaction supporting each explanation. An unresolved
   call remains unknown rather than becoming a plausible invented step.
4. Write a behavior specification with inputs, outputs, shared state, errors
   and boundary cases. Keep the specification separate from decompiled text
   so another implementation can work from the behavior description.
5. Compare reference and candidate on identical inputs. Locate the first
   difference and choose the next experiment from that difference. Test the
   comparator with deliberately wrong data and branches before trusting it.
6. Use [the local example](references/scenario.json) to distinguish a rule,
   an observed result and a negative control. Replace its example values
   with the actual task's inputs; it is not an implementation template.

## Pitfalls

- A source graph does not establish runtime equivalence.
- Normalizing an address must not conceal a different referenced value.
- Passing isolated functions does not prove shared state and initialization.

## Verification

- Each confirmed rule links to a local observation of the selected revision.
- Normal and boundary inputs have explicit expected outputs.
- Deliberately incorrect candidates fail the comparison.
- Unknowns remain visible in the specification and final explanation.

## Provenance

Method reference: https://github.com/morluto/rea at
4f7b5fb6023ce2ec449561782d09e3c7e5ac7c7b (MIT).
Original condensed procedure for native local tools; no external runtime.
