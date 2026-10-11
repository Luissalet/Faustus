"""The per-app MCP token and the ``data/url`` file the bridge reads.

Standard library only. The Node twin is ``readOrCreateToken`` / ``checkBearer`` in ``js/hoard-commons/server.js``.

The token is **stable across restarts**. Seven apps used to regenerate and overwrite ``mcp-token`` on every start,
so a second instance (autostart, a double click) changed the file while the running one kept the old token in
memory and the bridge started answering ``401 Invalid MCP token``. :func:`read_or_create_token` only ever creates
a token when the file is missing, shorter than ``min_len`` or unreadable, and creates it atomically (when two
processes race, the one that lost reads the winner's token).
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

from .atomic import read_json, tmp_path_for, update_json, write_bytes_atomic, write_text_atomic

__all__ = ["read_or_create_token", "read_token", "write_url", "read_url", "check_bearer", "new_token", "bearer_of",
           "AGENT_TOKENS_FILE", "PROFILES", "hash_token", "mint_agent_token", "revoke_agent_tokens", "list_agent_tokens",
           "lookup_agent_token"]

PathLike = Union[str, "os.PathLike[str]"]


def new_token(min_len: int = 32) -> str:
    """A URL-safe random token of at least ``min_len`` characters (43 for the default)."""
    return secrets.token_urlsafe(max(32, math.ceil(min_len * 3 / 4) + 1))


def read_token(path: PathLike, *, min_len: int = 32) -> Optional[str]:
    """The stored token, or ``None`` when the file is missing, unreadable or shorter than ``min_len``."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            value = fh.read().strip()
    except (OSError, UnicodeError):
        return None
    return value if len(value) >= min_len and not any(c.isspace() for c in value) else None


def read_or_create_token(path: PathLike, *, min_len: int = 32) -> str:
    """The token stored at ``path``; a new one (written atomically, mode 0600 where supported) when the file is
    missing, unreadable or shorter than ``min_len``. Calling it again, from this or another process, returns the
    same value."""
    existing = read_token(path, min_len=min_len)
    if existing:
        return existing
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = new_token(min_len)
    if target.exists():
        again = read_token(path, min_len=min_len)       # somebody created it between our read and now
        if again:
            return again
    else:
        # create-if-absent, atomically and with the full content: link the finished temp file in. Whoever links
        # first wins; the loser adopts the winner's token instead of overwriting it.
        tmp = tmp_path_for(target)
        try:
            write_bytes_atomic(tmp, token.encode("ascii"), mode=0o600)
            try:
                os.link(tmp, target)
                return token
            except FileExistsError:
                again = read_token(path, min_len=min_len)
                if again:
                    return again
            except OSError:
                pass  # no hard links here (FAT, some network shares): fall back to the plain replace below
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    write_bytes_atomic(target, token.encode("ascii"), mode=0o600)
    return token


def write_url(path: PathLike, url: str) -> None:
    """Record the URL this instance listens on (``data/url``), atomically."""
    write_text_atomic(path, str(url).strip(), fsync=False)


def read_url(path: PathLike) -> Optional[str]:
    """The recorded URL, or ``None`` when the file is missing or empty."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            value = fh.read().strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def check_bearer(header: Optional[str], token: Optional[str]) -> bool:
    """True when ``header`` is ``Bearer <token>`` (scheme case-insensitive, constant-time comparison). An empty
    token never matches, whatever the header says."""
    if not token or not header:
        return False
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    candidate = rest.strip()
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), token.encode("utf-8"))


def bearer_of(header: Optional[str]) -> str:
    """The token in an ``Authorization: Bearer <token>`` header ('' when it is not one)."""
    if not header:
        return ""
    scheme, _, rest = header.strip().partition(" ")
    return rest.strip() if scheme.lower() == "bearer" else ""


# ------------------------------------------------------------------------------------------------ agent tokens
#
# Besides its main token (``mcp-token``: every tool, every route) an app can accept extra tokens that carry a
# *profile* and the id of the agent they were given to. They live in ``<data>/agent_tokens.json`` as
# ``{sha256(token): {"agent", "profile", "label", "created"}}``: the file holds only hashes, the token itself is shown
# once when it is minted. ``agentkit.make_agent_router(data_dir=...)`` reads the file on every call (cached by mtime),
# so minting or revoking takes effect without restarting the app.
#
#   python -m hoard_link.tokens mint   --app-data-dir DIR --agent codex-sparks --profile drafts [--label TEXT]
#   python -m hoard_link.tokens list   --app-data-dir DIR
#   python -m hoard_link.tokens revoke --app-data-dir DIR (--id ID | --agent AGENT)

AGENT_TOKENS_FILE = "agent_tokens.json"
#: ``read_only`` calls only read tools; ``drafts`` also calls the tools flagged ``draft_safe``; ``all`` is a full scope.
PROFILES = ("read_only", "drafts", "all")
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,79}$")
_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def hash_token(token: str) -> str:
    """The SHA-256 hex digest an agent token is stored under."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _tokens_path(data_dir: PathLike) -> Path:
    return Path(data_dir) / AGENT_TOKENS_FILE


def _public(token_hash: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {"id": token_hash[:12], "agent": entry.get("agent", ""), "profile": entry.get("profile", "read_only"),
            "label": entry.get("label", ""), "created": entry.get("created")}


def _private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def mint_agent_token(data_dir: PathLike, agent: str, profile: str = "drafts", *, label: str = "") -> dict[str, Any]:
    """Create a token for ``agent`` with ``profile``. Returns ``{"token", "id", "agent", "profile", "label", "created"}``:
    the token appears only here; the file keeps its hash."""
    agent = str(agent or "").strip()
    if not _AGENT_ID.match(agent):
        raise ValueError("agent must be 1-80 characters: letters, digits and _ . : @ - (it starts with a letter or digit)")
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(PROFILES)}")
    token = "hat_" + secrets.token_urlsafe(32)
    token_hash = hash_token(token)
    entry = {"agent": agent, "profile": profile, "label": str(label or "")[:120], "created": round(time.time(), 3)}
    path = _tokens_path(data_dir)
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    def add(doc: Any) -> Any:
        doc = doc if isinstance(doc, dict) else {}
        doc[token_hash] = entry
        return doc

    update_json(path, add, default={}, indent=2, sort_keys=True)
    _private(path)
    return {"token": token, **_public(token_hash, entry)}


def list_agent_tokens(data_dir: PathLike) -> list[dict[str, Any]]:
    """The tokens of the app without their secrets (``id`` is the first 12 characters of the hash)."""
    doc = read_json(_tokens_path(data_dir), {})
    if not isinstance(doc, dict):
        return []
    rows = [_public(h, e) for h, e in doc.items() if isinstance(e, dict)]
    return sorted(rows, key=lambda r: (r["created"] or 0, r["id"]))


def revoke_agent_tokens(data_dir: PathLike, *, token_id: Optional[str] = None, agent: Optional[str] = None) -> int:
    """Remove the token whose ``id`` starts with ``token_id`` (at least 8 characters, and only one may match), or every
    token of ``agent``. Returns how many were removed."""
    if not token_id and not agent:
        raise ValueError("give a token id or an agent")
    removed = {"n": 0}

    def drop(doc: Any) -> Any:
        doc = doc if isinstance(doc, dict) else {}
        if token_id:
            if len(token_id) < 8:
                raise ValueError("the token id must have at least 8 characters")
            hits = [h for h in doc if h.startswith(token_id.lower())]
            if len(hits) > 1:
                raise ValueError("that id matches more than one token")
        else:
            hits = [h for h, e in doc.items() if isinstance(e, dict) and e.get("agent") == agent]
        for h in hits:
            del doc[h]
        removed["n"] = len(hits)
        return doc

    path = _tokens_path(data_dir)
    if not path.is_file():
        return 0
    update_json(path, drop, default={}, indent=2, sort_keys=True)
    _private(path)
    return removed["n"]


def lookup_agent_token(data_dir: PathLike, token: str) -> Optional[dict[str, Any]]:
    """``{"agent", "profile", "label", "id"}`` when ``token`` is one of the app's agent tokens, else ``None``. The file
    is re-read when it changes."""
    if not token:
        return None
    path = _tokens_path(data_dir)
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is None or hit[0] != sig:
        doc = read_json(path, {})
        doc = doc if isinstance(doc, dict) else {}
        with _cache_lock:
            _cache[key] = (sig, doc)
    else:
        doc = hit[1]
    digest = hash_token(token)
    entry = doc.get(digest)
    if not isinstance(entry, dict) or entry.get("profile") not in PROFILES:
        return None
    return {"id": digest[:12], "agent": entry.get("agent", ""), "profile": entry["profile"], "label": entry.get("label", "")}


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="python -m hoard_link.tokens", description="Mint, list and revoke the agent tokens of one Hoard app.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    parsers = {name: sub.add_parser(name, help=text) for name, text in
               (("mint", "create a token (shown once)"), ("list", "list the tokens (no secrets)"), ("revoke", "remove tokens"))}
    for each in parsers.values():
        each.add_argument("--app-data-dir", required=True, help="the app's data folder (the one holding mcp-token)")
    parsers["mint"].add_argument("--agent", required=True)
    parsers["mint"].add_argument("--profile", choices=PROFILES, default="drafts")
    parsers["mint"].add_argument("--label", default="")
    parsers["revoke"].add_argument("--id", help="the id `list` shows (8 characters or more)")
    parsers["revoke"].add_argument("--agent", help="revoke every token of this agent")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "mint":
            out: Any = mint_agent_token(args.app_data_dir, args.agent, args.profile, label=args.label)
        elif args.cmd == "list":
            out = list_agent_tokens(args.app_data_dir)
        else:
            out = {"revoked": revoke_agent_tokens(args.app_data_dir, token_id=args.id, agent=args.agent)}
    except ValueError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
