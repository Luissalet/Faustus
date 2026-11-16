---
name: codebase-deepening
description: Find where a codebase is hard to change because its modules are shallow, and propose deepening refactors in a fixed vocabulary - module, interface, depth, seam, adapter, leverage, locality - using the deletion test, then grill the chosen candidate. Use when a codebase feels hard to navigate or test, when asked to improve its architecture, or before designing or restructuring a module's interface.
version: 1.0.0
category: engineering
tags: [architecture, refactoring, modules, design, testability, third-party]
status: published
source: imported
---

## When to Use

Architecture review of an existing codebase, or the design of one module's interface: friction when following one
concept across many small files, logic that is only tested through tiny extracted helpers while the bugs live in
how they are called, tightly coupled modules that leak across their edges. Not for a bug fix, a feature, or a
rename. It proposes candidates and designs the chosen one; it does not rewrite the codebase unasked. Adapted from
https://github.com/mattpocock/skills (MIT, © 2026 Matt Pocock).

## Procedure

1. **Use one vocabulary, exactly.** *Module*: anything with an interface and an implementation, at any scale.
   *Interface*: everything a caller must know - signature, invariants, ordering, error modes, configuration,
   performance. *Depth*: behaviour a caller or test can exercise per unit of interface learned; deep = much behind
   a small interface, shallow = interface nearly as complex as the implementation. *Seam*: where an interface
   lives and behaviour can change without editing in place. *Adapter*: a concrete thing that fills a seam.
   *Leverage*: what callers gain from depth. *Locality*: change, bugs and knowledge concentrated in one place.
   Do not drift into "component", "service", "API" or "boundary"; the word is what keeps the argument precise.
2. **Decide where to look before looking.** If the person named a module, a subsystem or a pain point, start
   there. Otherwise read the history (`git log --oneline`, files changed most often) and let the hot spots pull
   attention first: a deepening pays off where change keeps happening. If the changes are scattered, widen the
   net. Read the project's glossary and decision records for that area first; they name good seams and list
   decisions not to re-litigate.
3. **Walk the code and note friction.** Where does understanding one concept mean bouncing between many small
   modules? Where is the interface nearly as large as the body? Where were pure functions extracted only to be
   testable, leaving the real bugs in how they are called? Where do coupled modules leak across their seams?
   What is untested, or hard to test through its present interface? For a large codebase get the module map and
   execution flows from `code_graph_communities` and `code_graph_flows` instead of reading everything; have a
   fresh-context worker (`delegate_agents`, read-only) do the walk so this conversation stays small.
4. **Apply the deletion test to each suspect.** Imagine deleting the module. If the complexity vanishes, it was a
   pass-through; if it reappears across N callers, it was earning its keep. "Concentrates complexity" in the
   shallow candidate is the signal you want.
5. **Present candidates, no interfaces yet.** For each: the files involved; the problem as friction; the change
   in plain words; the benefit stated as locality, leverage and how the tests improve; a before/after sketch
   (a short outline is enough); and a strength - strong, worth exploring or speculative. Name things with the
   project's own domain terms. If a candidate contradicts a recorded decision, show it only when the friction is
   real enough to reopen it, and say so. End with the one you would do first and why. Then ask which to explore.
6. **Grill the chosen one.** Walk its decision tree with the person (see `pretask-interview`): constraints,
   dependencies, the shape of the deepened module, what sits behind the seam, which tests survive. Update the
   project glossary as terms crystallise; when a candidate is rejected for a reason a future reviewer would need,
   offer to record it as a decision so it is not suggested again. Skip ephemeral reasons.
7. **Design the interface more than once.** For the chosen module, produce two or three radically different
   interfaces (smallest possible surface; most flexible; optimised for the commonest caller), ideally by
   separate read-only workers, and compare them on depth, locality and seam placement before choosing.
8. **Design for tests.** Accept dependencies instead of creating them; return results instead of mutating; keep
   the surface small. The interface is the test surface: if you must test past it, the module is the wrong shape.
   Add a seam only where something actually varies: one adapter is a hypothetical seam, two make it real. When
   deepening, replace tests of the old shallow modules with tests at the new interface rather than layering them.

## Pitfalls

- Using the loose words (service, component, boundary); every candidate then sounds the same.
- Proposing interfaces before the person has picked a candidate.
- Treating depth as lines of implementation per line of interface; that rewards padding. Depth is leverage.
- Adding a seam, interface or adapter for a variation that does not exist yet.
- Scanning the whole repository evenly instead of following where change actually happens.
- Listing every theoretical refactor a recorded decision forbids.
- Deleting a module that looked like a pass-through without running the deletion test against its callers.

## Verification

- Every candidate names its files, the friction, the benefit in locality and leverage terms, and a strength.
- Each shallow-module claim has passed the deletion test, and the result is stated.
- No interface was proposed before the person picked a candidate.
- The chosen design lists what sits behind the seam, which adapters exist today, and which tests move to the new
  interface.
- Glossary and decision records were read first, and any contradiction with them is flagged, not hidden.
