"""Owner-only permissions for the files on disk that *are* credentials.

SEC-1 (B-009, B-010, B-020). New writes go through
``atomic_write_json(..., private=True)``, which creates the file locked down
from its first byte. An install that predates SEC-1 already has ``auth.json``,
``sessions.json`` and ``data/mcp_oauth/*`` sitting on disk with whatever the
umask (POSIX) or the parent directory's inherited ACL (Windows) handed out.
Their *contents* are fine; only the permissions are wrong, so this pass fixes
the permissions and never rewrites a byte.

Set ``FAUSTUS_SKIP_SECRET_HARDENING=1`` to skip it — the escape hatch for a
deployment where a second account (a backup service, a different service user)
legitimately needs to read the data directory.
"""

from __future__ import annotations

import logging
import os
import time

from core.platform_compat import restrict_dir_to_owner, restrict_to_owner
from src.constants import (
    APP_KEY_FILE,
    AUTH_FILE,
    INTEGRATIONS_FILE,
    MCP_OAUTH_DIR,
    SESSIONS_FILE,
    VAULT_FILE,
)

logger = logging.getLogger(__name__)

#: Files whose leak is immediately exploitable: keys, sessions, second factors.
SECRET_FILES = (
    APP_KEY_FILE,
    AUTH_FILE,
    SESSIONS_FILE,
    INTEGRATIONS_FILE,
    VAULT_FILE,
)

#: Directories that contain nothing but credentials, walked one level deep.
SECRET_DIRS = (MCP_OAUTH_DIR,)


def harden_secret_files(files=None, dirs=None) -> dict:
    """Restrict every existing secret file/dir to the owner. Returns a report.

    Never raises: a permission pass that crashes startup would be worse than
    the exposure it fixes. Paths that do not exist are counted as `missing`,
    which is the normal case for a fresh install.
    """
    if os.getenv("FAUSTUS_SKIP_SECRET_HARDENING", "").strip().lower() in ("1", "true", "yes"):
        return {"skipped": True, "restricted": 0, "failed": 0, "missing": 0}

    started = time.monotonic()
    report = {"skipped": False, "restricted": 0, "failed": 0, "missing": 0}

    def _apply(path: str, is_dir: bool) -> None:
        if not os.path.exists(path):
            report["missing"] += 1
            return
        ok = restrict_dir_to_owner(path) if is_dir else restrict_to_owner(path)
        report["restricted" if ok else "failed"] += 1

    for path in files if files is not None else SECRET_FILES:
        _apply(path, is_dir=False)

    for directory in dirs if dirs is not None else SECRET_DIRS:
        _apply(directory, is_dir=True)
        if not os.path.isdir(directory):
            continue
        for root, _subdirs, names in os.walk(directory):
            for name in names:
                _apply(os.path.join(root, name), is_dir=False)

    report["seconds"] = round(time.monotonic() - started, 3)
    logger.info(
        "Secret file hardening: %s restricted, %s failed, %s missing (%ss)",
        report["restricted"], report["failed"], report["missing"], report["seconds"],
    )
    return report
