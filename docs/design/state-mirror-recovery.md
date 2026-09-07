# Verified State Mirror recovery

Open an entity in State Mirror and choose **Verify saved state**. This compares
the materialized view with a checksum-verified reconstruction. If they differ,
**Restore verified state** repairs only that derived view. It does not undo files,
restart services, change objectives, or replace their authoritative sources.

`POST /api/state/rebuild` accepts `entity_id` and defaults to verification only.
Applying requires `apply: true` and the `expected_sha256` from the preview receipt.
It is human/admin-only, resolves the owner from the request, hides foreign entities,
and rejects a stale receipt. Verification and repair share a SQLite write lock;
repair advances the change cursor so consumers reload it. No probing is required.

Each materialized write records its actual transition in the same transaction,
including field provenance, timestamps, revision and conflict references. Recovery
replays these transitions, not today's reducer against a partial list of probes:
that would change freshness decisions and omit later conflict reconciliation.
Raw observation retention therefore cannot invalidate recovery. Every 100 steps,
the entire retained chain is verified and folded into a new checkpoint atomically.
This preserves full latest-state recovery, not unlimited historical time travel.

Pre-upgrade entities acquire an explicitly labelled `legacy_checkpoint` on their
next materialized write. Without a journal, recovery refuses rather than inventing
missing history. The checkpoint cannot prove observations lost before this upgrade.
Checksums detect corruption and gaps; they are not signatures against someone with
write access to the database. A damaged journal never replaces the live state.

Implementation: `src/state_mirror/replay.py`, `StateStore.put_state/rebuild_state`
in `persistence.py`, `/api/state/rebuild` in `routes/state_mirror_routes.py`, and the
existing State Mirror detail panel. Regression coverage: `test_state_mirror_replay`
and `test_state_mirror_routes`.
