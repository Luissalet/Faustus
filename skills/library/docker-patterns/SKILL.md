---
name: docker-patterns
description: Multi-stage builds that keep the production image minimal, a non-root user, pinned base image tags, and compose overrides that separate dev from prod config. Use when writing or reviewing a Dockerfile or docker-compose setup.
version: 1.0.0
category: engineering
tags: [docker, containers, deployment]
status: published
source: imported
---

## When to Use

Writing or reviewing a `Dockerfile`, `docker-compose.yml`, or the
containerisation of a service for local development or deployment.

## Procedure

1. **Use multi-stage builds**: a build/dependency stage that installs
   toolchains and compiles, and a slim final stage that copies only the
   built artifact and runtime dependencies — the production image should
   not carry a compiler, dev dependencies, or build cache it will never use
   again.
2. **Pin base image tags to a specific version, never `:latest`.** A build
   that resolves `:latest` at build time is not reproducible: the same
   Dockerfile can produce a different image tomorrow with no change to the
   repository.
3. **Run as a non-root user in the final image.** Create a dedicated user
   in the Dockerfile and switch to it before `CMD`/`ENTRYPOINT` — a
   container running as root that gets compromised hands the attacker root
   inside the container, and sometimes on the host depending on
   configuration.
4. **Order layers for cache efficiency**: copy dependency manifests
   (`requirements.txt`, `package.json`) and install dependencies before
   copying the rest of the source, so a source-only change doesn't
   invalidate the (usually much slower) dependency-install layer.
5. **Use compose override files to separate dev from prod**: a base
   `docker-compose.yml` with what's always true, an auto-loaded
   `docker-compose.override.yml` for dev-only conveniences (hot reload,
   mounted source, debug ports), and an explicit `docker-compose.prod.yml`
   for production-only settings — never one file with commented-out blocks
   for the other environment.
6. **Expose only what's needed.** A service that other containers reach
   over the compose network doesn't need its port published to the host at
   all; publish only what an operator outside the compose network actually
   needs to reach.
7. **Use named volumes for anything that must survive a container
   recreation** (a database's data directory) and bind mounts only for
   what you genuinely want to edit live from the host (source code in
   dev).
8. **Add a health check** for anything another service depends on starting
   up successfully — `depends_on` alone only waits for the container to
   start, not for the process inside it to be ready.

## Pitfalls

- `FROM something:latest`, silently changing what a rebuild produces months
  after the Dockerfile was written.
- Copying the entire source tree before installing dependencies, so every
  code change reinstalls every dependency from scratch.
- Running the container's main process as root because it was the path of
  least resistance during development and nobody circled back.
- One `docker-compose.yml` with dev settings hardcoded and a comment saying
  "change this for production" — the comment gets missed exactly once and
  that's the incident.
- A secret baked into an image layer (even one later removed in a
  subsequent layer) — it's still recoverable from the image history.

## Verification

- The final production image does not contain build tools, dev
  dependencies, or source files beyond the built artifact — checked with
  `docker history`/`docker inspect`, not assumed from the Dockerfile alone.
- The container runs as a non-root user, verified with `docker exec ...
  whoami` (or equivalent) against a built image, not just read from the
  Dockerfile.
- A rebuild from a clean cache with the same Dockerfile produces the same
  base image digest (pinned, not `:latest`).
- Dev-only conveniences (mounted source, debug ports) do not appear in the
  production compose file.
