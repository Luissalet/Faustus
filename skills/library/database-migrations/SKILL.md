---
name: database-migrations
description: Additive-first, zero-downtime-oriented migrations — a new column arrives nullable, a rename is add-then-backfill-then-drop, and every migration is reviewed for lock behaviour on a large table. Use when writing or reviewing a schema migration for a database that has real data in it (i.e. not a brand-new empty dev database).
version: 1.0.0
category: engineering
tags: [database, migrations, schema]
status: published
source: imported
---

## When to Use

Any change to a database schema once the table has (or will soon have) real
data and more than one deployed version of the application might read it
concurrently — which, in practice, is almost every schema change past the
prototype stage.

## Core Principles

Migrations should be **additive before destructive**: add the new thing,
migrate the data, only then remove the old thing — in separate migrations,
deployed separately, so a rollback of the application code doesn't leave
the schema in a state the old code can't read. And they should be
**reversible or explicitly justified when not**: a migration that can't be
undone (a destructive drop, an irreversible data transform) should say so
and say why the risk is accepted.

## Procedure

1. **Adding a column**: add it nullable (or with a default) first, deploy,
   backfill in batches if the table is large, then — in a later migration —
   add a `NOT NULL` constraint once every row is populated. Adding a
   `NOT NULL` column with no default to a large table locks it for the
   scan; avoid doing both in one step.
2. **Adding an index on a large table**: use the database's non-blocking
   index creation (`CREATE INDEX CONCURRENTLY` on Postgres, or the
   equivalent online DDL for the engine in use) rather than a plain blocking
   `CREATE INDEX`.
3. **Renaming a column** as three separate steps, each its own deploy: add
   the new column, dual-write (or backfill then have the app read the new
   one), then drop the old column once nothing reads it anymore. Never
   rename in place while the old application code is still running against
   it.
4. **Removing a column**: confirm nothing in the current or immediately
   prior deployed version still reads it, then drop it in its own
   migration — not bundled with unrelated schema changes, so a problem is
   easy to isolate.
5. **Large data migrations/backfills**: run in bounded batches with a
   pause between them, not a single unbounded `UPDATE` — an unbounded
   update on a large table holds locks and generates replication/WAL
   pressure for the whole duration.
6. **Check every migration for lock behaviour** on the table's actual size
   in production, not its size in a local dev database — an operation
   that's instant on a thousand rows can lock a production table with
   millions for long enough to cause visible downtime.
7. **Review the generated migration**, whatever ORM/tool produced it —
   generated SQL is a starting point, not something to trust blindly for a
   sensitive change (a data type change, a large backfill).

## Pitfalls

- Adding a `NOT NULL` column with no default directly to a table with
  existing rows — the migration fails outright, or locks the table for a
  full-table rewrite depending on the engine.
- Renaming a column in one migration while the currently-deployed
  application code still reads the old name — the deploy that runs the
  migration first (before the new app code rolls out) breaks the running
  service.
- An unbounded backfill `UPDATE` on a large, actively-written table run
  during business hours.
- Dropping a column in the same migration that also does something else,
  making a bad migration harder to bisect and roll back cleanly.
- Trusting an ORM's auto-generated migration for a destructive or
  data-type-changing operation without reading the actual SQL it produces.

## Verification

- Every migration was checked against the real (or a realistic-sized)
  table for lock duration, not just correctness on an empty dev database.
- A column rename or drop was split into the add/backfill/drop sequence,
  each a separate migration, each independently deployable.
- The migration has an explicit rollback path, or a written justification
  for why it doesn't.
- A large backfill runs in bounded batches, and was tested against a
  realistic row count before being scheduled against production.
