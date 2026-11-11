---
name: plate-thermal-stress-check
description: Set up a steady heat or plane-stress problem on a plate-like part with stated conditions, check it against an analytical reference and mesh refinement, and compare variants. Use when a flat part needs a thermal or stress estimate whose reliability must be shown.
version: 1.0.0
category: testing
tags: [fem, simulation, heat-conduction, plane-stress, plate, mesh, verification, engineering, simulacion, calor, tension, ingenieria]
apps: [maxwell]
status: published
source: imported
---

## When to Use

You have a flat or plate-like part and need steady-state temperatures, or stresses and deflections under in-plane loads, with an honest idea of how far to trust the numbers. It covers setup, verification and variant comparison. It does not cover transient heat, buckling, contact, 3D bulk parts, or certification of a safety-critical part.

## Procedure

1. Read the app's tool list. Write the problem as data before computing: geometry with units, thickness, material properties (conductivity, or modulus and Poisson ratio) with their source, and for each edge exactly one condition (fixed temperature, flux, convection with coefficient and ambient, or a support and a load). A condition missing on an edge is the commonest mistake.
2. Check the model is well posed. For heat, at least one edge must fix temperature or lose heat by convection; for stress, the supports must prevent rigid motion. Confirm the assumption fits: plane stress holds for thin plates loaded in their plane.
3. Run on a coarse mesh and then on at least two finer meshes. Compare the quantity you actually care about (maximum temperature, maximum stress, a deflection at a point), not only a global value. If it keeps changing by more than your tolerance, refine more, and expect stress peaks near corners and holes to keep rising; report such a peak as unresolved rather than as a result.
4. Compare with an analytical reference on a simplified case that has a known answer: a uniform bar for conduction, a plate with a hole under tension for the stress concentration factor, a cantilever for deflection. Run the same code path on that case and record the error.
5. Check the balances the app reports: heat in equals heat out; applied forces equal reactions. A violation means a setup error, not a rounding issue.
6. Read the result physically. Do temperatures lie between the boundary values? Is the hottest place where the sources and insulation say it should be? Is the deformation in the direction of the load?
7. Compare variants (thicker plate, different material, moved hole, added fin) under identical conditions and mesh settings. Change one thing at a time and report the difference with its mesh uncertainty. If a design search is run, test the winner on conditions not used during the search.
8. Report the conditions, mesh study, reference error, balances, the result and its stated limits.

## Pitfalls

- Mixing units, especially millimetres with pascals.
- Taking a singular stress at a sharp corner as a real peak.
- Accepting a result from a single mesh.
- Convection coefficients assumed without a source.
- Comparing variants whose mesh quality differs.
- Reporting a design as optimal on the same data used to search.

## Verification

- Every edge has a stated condition and every property has a unit and source.
- The key quantity changes by less than the stated tolerance between the two finest meshes, or the report says it does not.
- The analytical case reproduced its reference within a stated error.
- Energy and force balances are satisfied.
