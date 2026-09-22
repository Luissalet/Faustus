---
id: react/testing
title: React testing
applies_to: [TypeScript/React, JavaScript/React]
priority: 36
summary: Test behaviour a user would see, query by role/text, wait for state not a timer.
---

- Query elements the way a user would find them (role, label, visible
  text), not by CSS class or internal structure.
- Test observable behaviour (what renders, what happens on interaction),
  not internal state or implementation detail.
- Wait for an actual condition (an element appearing, a loading state
  clearing) rather than a fixed-duration sleep.
- Mock the network/data layer at its boundary; render the real component
  tree above it.
- Reset any shared test state between tests so order doesn't affect the
  result.
