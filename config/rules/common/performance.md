---
id: common/performance
title: Performance defaults
applies_to: []
priority: 24
summary: Measure before optimizing; fix the algorithm before the micro-detail.
---

- Profile or measure before optimizing — don't guess at the bottleneck and
  rewrite the wrong function.
- Fix an O(n²) or repeated-query pattern before micro-optimizing a hot
  loop's constant factor; the algorithmic fix usually dominates.
- Watch for a query or a request issued once per item in a loop (N+1) where
  a single batched call would do.
- Don't add a cache, a memoization, or a lock without a measured problem to
  justify the added complexity and its own new failure modes.
- Keep a lock's held section as small as possible; never hold one across a
  network call, an await point, or a long computation.
- Prefer streaming/pagination over loading an unbounded result set into
  memory at once.
