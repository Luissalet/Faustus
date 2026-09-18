# Faustus distribution lifecycle (A35)

The five stages a real install goes through, what's automated today, and
what stays a manual documented procedure because it needs a clean machine
this repo's tests do not have.

## 1. Clean install

- **Windows** — native installer, or `launch-windows.ps1` from a checked-
  out copy (creates a venv, installs requirements, starts the app).
- **macOS** — `launcher.py` (creates a venv, installs requirements, starts
  the app) or Docker Compose.
- **Docker** — `docker compose up -d --build` (see `docker-compose.yml`,
  `update_windows.bat`'s Docker-deployment counterpart).

All three converge on the same layout: a `data/` directory holding every
persisted file/DB (`src/constants.py::DATA_DIR`, default `<repo>/data`),
and an optional `.env` for secrets/overrides. **Manual step, not automated
here:** actually running an installer on a machine with nothing on it —
this repo's CI/tests run inside an existing checkout and cannot simulate
"nothing installed yet". What IS automated: the backup/restore round trip
below, and the uninstall scripts' safety property.

## 2. Upgrade

- **Windows (Docker deployment)** — `update_windows.bat`: checks for
  `git`/`docker`/`docker compose` on PATH, `git pull --ff-only`, then
  `docker compose up -d --build`. Never touches `data/`.
- **Windows (native)/macOS** — `git pull --ff-only` (or re-run the
  installer with the newer source) then re-run `launch-windows.ps1` /
  `launcher.py`, which reuse the existing `data/` untouched — nothing in
  either script deletes or recreates `data/` on a re-run.
- **Docker** — `docker compose up -d --build` reuses the named volume /
  bind-mounted `data/` from the previous run.

## 3. Backup

Two real tools, same `data/…` archive shape so either can read the
other's output:

- `scripts/odysseus-backup snapshot` — the shipped CLI: tars `data/`
  (SQLite DBs copied via `.backup()` so a live app can't corrupt the
  snapshot), verifies the archive, supports encryption
  (`src/backup_crypto.py`) and profiles (`src/backup_service.py`).
- `scripts/lifecycle_check.py backup --data-dir D --out B.zip` — this
  lot's own tool, used by its test to exercise a real backup+restore round
  trip against a throwaway `DATA_DIR` without touching the real repo's
  `data/`: zips every file under `D`, with a manifest of relative path →
  sha256 written inside the archive.

## 4. Restore

- `scripts/odysseus-backup restore PATH --yes` — the shipped CLI. Requires
  `--yes` (restore is destructive: it overwrites `data/` in place, first
  moving the current one aside to `data.before-restore-<timestamp>/`).
  Deliberately has no HTTP endpoint (`src/backup_service.py`'s module
  docstring): restoring under a running app with open SQLite handles turns
  one problem into two.
- `scripts/lifecycle_check.py restore --zip B.zip --data-dir D [--overwrite]`
  — unzips into `D`, then re-hashes every restored file against the
  archive's manifest and reports `all_verified`. Refuses to restore into a
  `D` that already has files unless `--overwrite` is passed, so a restore
  never silently clobbers newer data.

**User-data preservation is an explicit choice, not a default guess:**
neither restore tool ever overwrites existing data without either `--yes`
(shipped CLI, which also keeps the pre-restore copy) or `--overwrite`
(this lot's tool).

## 5. Uninstall

- `scripts/uninstall.sh` (Linux/macOS) and `scripts/uninstall.ps1`
  (Windows) — both new in this lot. Default (no flags): stop/remove the
  service or scheduled task; **`data/`, `.env` and `backups/` are left
  alone.** Only `--purge` (`-Purge` on Windows) deletes them, and even
  then asks for a typed `yes` confirmation unless `--yes`/`-Yes` is also
  given.
- `scripts/lifecycle_check.py check-uninstall-scripts` — a static
  safety check: both scripts exist, and any line in them that deletes
  `data/`/`.env`/`backups/` is guarded by the purge flag (`PURGE` in the
  bash script, `$Purge` in the PowerShell one) — an uninstall script that
  deletes user data with no guard at all fails this check.

## What this lot verified for real vs. documented only

| Step | Verified how |
|---|---|
| Backup → restore round trip, data preserved bit-for-bit | `tests/acceptance/test_a35_lifecycle.py` — real files on a `tmp_path` `DATA_DIR`, real zip, real sha256 comparison |
| Uninstall scripts exist and never delete `data/` without `--purge` | Same test file, via `check_uninstall_scripts()` |
| Clean install / upgrade on an actual clean Windows/macOS/Docker machine | **Not run here** — needs a machine this repo's test suite does not have. The scripts above are what a person or a CI runner with such a machine would execute; this lot did not have one to execute them against. |
