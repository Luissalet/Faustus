---
name: deployment-patterns
description: Rolling, blue-green, and canary deployment strategies, health checks that actually gate traffic, and a CI/CD pipeline shaped as build-test-deploy-verify. Use when setting up or reviewing how a service gets deployed and how a bad deploy gets caught before — or rolled back after — it reaches everyone.
version: 1.0.0
category: engineering
tags: [deployment, cicd, infrastructure]
status: published
source: imported
---

## When to Use

Setting up a deployment pipeline for a new service, or reviewing an
existing one after an incident revealed a gap (a bad deploy that reached
100% of traffic before anyone noticed, or a rollback that didn't actually
roll back).

## Deployment Strategies

**Rolling** (the default for most services): replace instances gradually,
a few at a time, checking health before continuing to the next batch. Low
overhead, works for most stateless services, but a bad version is briefly
live on some fraction of instances during the rollout.

**Blue-green**: run the new version fully alongside the old, switch traffic
over once it's verified, keep the old version standing by for an instant
rollback. Costs double the infrastructure during the switch window; buys a
rollback that's just flipping traffic back, not a redeploy.

**Canary**: route a small percentage of real traffic to the new version,
watch real metrics (error rate, latency, business metrics if relevant), and
ramp up only if they hold — the right choice for a change risky enough that
you want real traffic evidence before committing everyone to it.

Pick based on risk and cost tolerance, not by default habit: rolling for
routine changes, canary for anything you're genuinely unsure about, blue-
green when an instant full rollback matters more than infrastructure cost.

## Procedure

1. **Build once, deploy the same artifact everywhere** (a container image
   built once and promoted through environments) — never rebuild per
   environment, which risks a subtly different artifact reaching
   production than the one that passed tests in staging.
2. **Gate the pipeline on real checks**: build succeeds, tests pass, and —
   before traffic-shifting — a health check confirms the new version is
   actually ready (not just "the process started"), including its own
   dependencies (database connection, required config present).
3. **Write a health check that reflects real readiness**: check that the
   things the service actually depends on to serve a request are reachable,
   not just that the HTTP server is listening. A liveness check and a
   readiness check answer different questions — don't route traffic to an
   instance that's alive but not yet ready.
4. **Automate the rollback path**, not just the forward deploy — if
   rolling back requires someone to remember the manual steps under
   pressure at 2am, it will go wrong at exactly the moment it matters most.
5. **Watch real signals during a canary/rollout**: error rate, latency,
   and anything specific to what this deploy changed — not just "the
   process is still running".
6. **Never deploy a schema-breaking change and the code that depends on it
   in the same rollout** without following the additive-first sequence
   from `database-migrations` — a rolling deploy runs old and new code
   concurrently against the same database for the duration of the rollout.

## Pitfalls

- A "health check" that only confirms the process started, not that its
  actual dependencies (database, required upstream service) are reachable
  — traffic gets routed to an instance that's up but broken.
- No automated rollback, so a bad deploy at 2am depends on someone
  remembering the exact manual sequence under pressure.
- Rebuilding the artifact separately per environment, so what passed
  staging isn't provably the same bytes running in production.
- Shipping a schema change and the code that requires it in one rolling
  deploy, breaking every instance still running the old code mid-rollout.
- Watching only "is it up" during a canary instead of the metrics that
  would actually reveal the specific risk this change introduces.

## Verification

- The exact artifact that passed tests is the one running in production —
  traceable by build ID/digest, not rebuilt along the way.
- The health check gating traffic actually exercises the service's real
  dependencies, verified by testing it against a deliberately broken
  dependency once.
- A rollback was actually exercised (in staging, or for real) and confirmed
  to restore the previous known-good state, not just assumed to work.
- Any accompanying schema change was deployed additive-first, ahead of the
  code that depends on it.
