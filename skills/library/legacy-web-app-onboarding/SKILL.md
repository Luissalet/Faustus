---
name: legacy-web-app-onboarding
description: Receive someone else's web prototype and turn it into a verified map - static audit, reproducible start, a feature inventory that becomes the no-regression contract, baselines and a short question list - before any rewrite. Use when a client or another team hands over an existing app to modernise, migrate or redesign.
version: 1.0.0
category: planning
tags: [onboarding, audit, inventory, migration, legacy, prototype, baseline, consultancy, auditoria, inventario, migracion, prototipo]
status: published
source: imported
---

## When to Use

Day one with an inherited codebase (a Python, Node or PHP prototype, a
designer's HTML, a script that grew into an app) that must become a product.
Use it before planning features or touching the interface. Not for a repo the
team already knows; `learn-this-repo` covers routine exploration.

## Procedure

1. **Isolate the copy.** Unpack into a new folder, initialise git and commit
   the material exactly as received. Never work on the original archive.
   Keep internal agent instruction files out of the client's history with
   `.git/info/exclude`, not `.gitignore`.
2. **Static audit without running anything.** Languages and line counts,
   declared and implied dependencies, frameworks, entry points, every route
   with method and file, front-end libraries and CDNs, 3D or media assets
   with sizes, files over 10 MB, tests (often none), `.env` files, likely
   secrets (report file and line, never the value), and smells: absolute
   paths, fixed localhost ports, `debug=True`, open CORS, `eval`.
3. **Make it start.** Create an isolated environment, install what is
   declared, add what imports require and note it. Start the detected entry
   point. If it fails, record the exact error instead of patching blindly.
4. **Inventory every function.** Walk the running app at phone and desktop
   widths and list each screen, action, form, export, shortcut and
   background job: what it does, where it lives, which data it uses, and its
   state (works, broken, partial). Add screenshots. This document is the
   contract: the modernisation may not remove a line of it without a
   recorded decision from the person who owns the client relationship.
5. **Baselines.** Lighthouse on two or three routes; load time and frame
   rate of any heavy view; sizes of the largest assets; a sample of real
   data volumes. Save them so later work is measured against facts.
6. **Ten questions at most** for the client, concrete and answerable: users
   and roles, identity provider, file formats, documents they produce, where
   the source of truth lives, branding and licences, hosting or data
   residency constraints.
7. **Plan from the map.** Split the work into tasks with explicit files,
   owners by seniority and verifiable acceptance criteria; make every task
   wait on the reception task so nobody starts early.

## Pitfalls

- Rewriting before the inventory exists; features vanish silently.
- Printing secrets found during the audit into reports or chat.
- Fixing the prototype during onboarding and losing the as-received state.
- Committing agent tooling files into the client's repository.
- Asking the client twenty vague questions instead of ten precise ones.
- Trusting the README over the running application.

## Verification

- The as-received commit exists and the working tree is clean afterwards.
- The audit report lists routes, libraries, assets and smells, with no
  secret values in it.
- The app starts from written instructions, or the blocking error is
  recorded verbatim.
- The inventory covers every screen reachable from the navigation, each with
  a screenshot and a state.
- Baselines and the question list are saved beside the audit.
