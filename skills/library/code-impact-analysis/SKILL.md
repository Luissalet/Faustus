---
name: code-impact-analysis
description: Trace a proposed change through imports, callers and tests before editing. Use when estimating change impact, renaming a symbol or investigating a regression.
version: 1.0.0
category: engineering
tags: [dependencies, imports, impact, graph, dependencias, impacto]
status: published
source: imported
---

## When to Use

Changing a public function, module, schema or configuration key whose users
may live outside the file being edited. The result is a justified set of
affected consumers and checks, with uncertainty stated explicitly.

## Procedure

1. Identify the repository revision and working changes. Locate the exact
   symbol and its qualified module, exports and signature. A shared name
   alone does not establish a dependency between two files.
2. Discover the available code index and refresh changed files. Resolve
   imports, aliases and relative module paths before collecting callers.
   Preserve the distinction between exact references, static inference
   and lexical matches; keep ambiguous candidates visible.
3. Traverse reverse dependencies from the changed symbol. Follow callers
   and re-exports, recording a path explaining why each consumer appears.
   Inspect dynamic registration, configuration and templates separately
   when the parser cannot establish their edges.
4. Connect affected consumers to their requirements and tests. Choose
   checks that exercise observable behavior, including a caller across a
   module boundary. Avoid adding tests that merely repeat implementation.
5. Apply the smallest sufficient change, refresh the index and repeat the
   query. Compare the affected set before and after a rename or removal.
   Report unresolved references rather than treating them as proof that
   nothing depends on the change.
6. Exercise [the import scenario](references/scenario.json) to distinguish
   a real consumer from an unrelated function with the same name.

## Pitfalls

- A stale index can miss the consumer most affected by the current edit.
- Name matching confuses homonyms and misses aliased imports.
- Broad dependency reach is a reason to inspect, not proof of breakage.

## Verification

- Every reported consumer has an explainable dependency path.
- An unrelated homonym does not become an exact caller.
- A rename refresh removes obsolete edges and preserves real consumers.
- Selected checks cover the behavior changed across the module boundary.

## Provenance

Method reference: https://github.com/Graphify-Labs/graphify at
86c57be8204673c2a1b9849a3292a7dd3ab373d8 (Apache-2.0 and retained notices).
Original procedure for the native local index; no external graph service required.
