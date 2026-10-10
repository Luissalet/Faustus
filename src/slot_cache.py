"""The KV state of a pinned document, saved on disk and restored on later turns.

A document pinned to a chat (`src/side_threads.py`, a "wired document
context" material) sits right after the system prompt, so every turn of the
chat starts with the same system + document tokens. llama-server keeps that
prefix in the chat's slot (`src/llama_slots.py`) and reuses it, until another
chat takes the slot or the server restarts: then the next turn reads the
document again, which on a long one costs seconds to minutes.

llama-server can write a slot's KV state to a file and read it back
(`--slot-save-path <dir>`; `POST /slots/{id}?action=save|restore` with a
filename). This module does that for the stable prefix of a chat:

  * `prepare` (before the request): when the prompt starts with a pinned
    document small enough (`llamacpp_slot_cache_max_tokens`), it works out a
    key from the model, the server's chat template, the tools and the exact
    prefix text. If the chat's slot already holds that key nothing is done;
    if a saved file for the key exists the slot is restored from it;
    otherwise the turn will process the prefix and be saved afterwards.
  * `finish` (when the answer is complete): reads the server's timings, checks
    the server really reused the prefix, saves the slot when it was a miss and
    returns the `prefix_cache` record the turn carries in its metrics.

What is saved is the whole slot after the turn (prefix, the question and the
answer): llama-server keeps the longest common prefix of the restored tokens
and the next prompt and drops the rest, so the document is not read again and
the earlier turn's tail may be reused too. A model that cannot drop the tail
(recurrent layers) re-reads the prompt, which `finish` sees in the timings and
reports as a miss.

Nothing here can fail a turn: every error ends in the normal path and is
logged once. A server that answers 501 (no `--slot-save-path`) is left alone
for `UNSUPPORTED_TTL` seconds. Files are named from the key, kept in one
folder with an index, and trimmed by age and total size (least recently used
first).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.llama_slots import server_base

logger = logging.getLogger(__name__)

#: `metadata.source` of a document material (src/side_threads.py builds it).
DOC_SOURCE_PREFIX = "wired document context: "

#: Under this a prefix is not worth a file; over `llamacpp_slot_cache_max_tokens`
#: the file would be too big to restore quickly.
MIN_PREFIX_TOKENS = 256

FILE_PREFIX = "fsc-"
FILE_SUFFIX = ".bin"
INDEX_NAME = "index.json"

UNSUPPORTED_TTL = 600.0
PROPS_TTL = 600.0
PROPS_FAIL_TTL = 60.0
SAVE_FAIL_RETRY_S = 300.0
#: How long the end of a turn waits for the save before letting it finish in
#: the background (the record then says `pending`).
SAVE_BUDGET_S = 2.0
PENDING_WAIT_S = 5.0
RESTORE_TIMEOUT_S = 30.0
SAVE_TIMEOUT_S = 60.0
PROBE_TIMEOUT_S = 2.0
#: A restored or warm slot that served less than this share of the estimated
#: prefix did not really keep it.
REUSE_FLOOR = 0.5

STATUSES = ("restored", "saved", "warm", "miss", "off")
#: Which status describes a turn made of several rounds (an agent turn).
STATUS_PRIORITY = {"restored": 5, "saved": 4, "miss": 3, "warm": 2, "off": 1}

_LOCK = threading.RLock()
_STATE: Dict[Tuple[str, int], Optional[Dict[str, Any]]] = {}
_PENDING: Dict[Tuple[str, int], "asyncio.Future"] = {}
_UNSUPPORTED: Dict[str, float] = {}
_PROPS: Dict[str, Tuple[float, str]] = {}
_SAVE_FAILED: Dict[str, float] = {}
_LOGGED: set = set()
_STATS: Dict[str, Any] = {s: 0 for s in STATUSES}
_STATS.update({"errors": 0, "prefill_saved_ms": 0.0})


# ── settings ────────────────────────────────────────────────────────────────

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        value = get_setting(key, default)
        return default if value is None else value
    except Exception:  # noqa: BLE001 - settings unavailable: the default
        return default


def enabled() -> bool:
    return bool(_setting("llamacpp_slot_cache", False))


def max_tokens() -> int:
    try:
        return max(MIN_PREFIX_TOKENS, int(_setting("llamacpp_slot_cache_max_tokens", 8000)))
    except (TypeError, ValueError):
        return 8000


def max_bytes() -> int:
    try:
        return max(1, int(_setting("llamacpp_slot_cache_max_mb", 4096))) * 1024 * 1024
    except (TypeError, ValueError):
        return 4096 * 1024 * 1024


def max_age_s() -> float:
    try:
        return max(1, int(_setting("llamacpp_slot_cache_max_age_days", 14))) * 86400.0
    except (TypeError, ValueError):
        return 14 * 86400.0


def cache_dir() -> str:
    """The folder llama-server was started with (`--slot-save-path`)."""
    configured = str(_setting("llamacpp_slot_cache_dir", "") or "").strip()
    if configured:
        return configured
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "slot_cache")


def _log_once(tag: str, message: str, *args: Any) -> None:
    with _LOCK:
        if tag in _LOGGED:
            return
        _LOGGED.add(tag)
    logger.warning(message, *args)


def reset() -> None:
    """Forget everything in memory (tests)."""
    with _LOCK:
        _STATE.clear()
        _PENDING.clear()
        _UNSUPPORTED.clear()
        _PROPS.clear()
        _SAVE_FAILED.clear()
        _LOGGED.clear()
        _STATS.clear()
        _STATS.update({s: 0 for s in STATUSES})
        _STATS.update({"errors": 0, "prefill_saved_ms": 0.0})


# ── the prefix ──────────────────────────────────────────────────────────────

def _is_pinned_document(message: Dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    meta = message.get("metadata")
    if isinstance(meta, dict) and str(meta.get("source") or "").startswith(DOC_SOURCE_PREFIX):
        return True
    content = message.get("content")
    return isinstance(content, str) and ("Source: " + DOC_SOURCE_PREFIX) in content[:800]


def pinned_prefix(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """The leading system messages and pinned documents of a prompt, and how
    many documents there are. It stops at the first message that is neither:
    what follows (notes, references, the history) is not the stable part."""
    prefix: List[Dict[str, Any]] = []
    docs = 0
    for message in messages or []:
        if not isinstance(message, dict):
            break
        if message.get("role") == "system":
            prefix.append(message)
        elif _is_pinned_document(message):
            prefix.append(message)
            docs += 1
        else:
            break
    return prefix, docs


def _serialise(prefix: List[Dict[str, Any]], tools: Any) -> str:
    rows = [[m.get("role"), m.get("content")] for m in prefix]
    return json.dumps({"messages": rows, "tools": tools or None},
                      ensure_ascii=False, sort_keys=True, default=str)


def prefix_key(model: str, server_fp: str, prefix: List[Dict[str, Any]], tools: Any) -> str:
    raw = json.dumps({"v": 1, "model": str(model or ""), "server": server_fp,
                      "prefix": _serialise(prefix, tools)}, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def filename_for(key: str) -> str:
    return f"{FILE_PREFIX}{key[:40]}{FILE_SUFFIX}"


# ── talking to the server ───────────────────────────────────────────────────

def _auth_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    return {k: str(v) for k, v in (headers or {}).items() if str(k).lower() == "authorization"}


async def _call(method: str, url: str, *, body: Optional[Dict[str, Any]] = None,
                params: Optional[Dict[str, str]] = None,
                headers: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        return await client.request(method, url, json=body, params=params,
                                    headers=_auth_headers(headers))


async def _server_fingerprint(base: str, headers: Optional[Dict[str, Any]]) -> str:
    """Model file and chat template the server reports in /props: a saved
    state is only good for the same weights and the same rendering."""
    now = time.monotonic()
    with _LOCK:
        cached = _PROPS.get(base)
    if cached and cached[0] > now:
        return cached[1]
    fingerprint = ""
    ttl = PROPS_FAIL_TTL
    try:
        resp = await _call("GET", base + "/props", headers=headers, timeout=PROBE_TIMEOUT_S)
        if resp.status_code == 200:
            props = resp.json()
            if isinstance(props, dict):
                path = str(props.get("model_path") or "").replace("\\", "/").rsplit("/", 1)[-1]
                template = str(props.get("chat_template") or "")
                fingerprint = f"{path}|{hashlib.sha1(template.encode('utf-8')).hexdigest()[:16]}"
                ttl = PROPS_TTL
    except Exception:  # noqa: BLE001 - the model name alone then identifies it
        logger.debug("slot cache: /props unreadable on %s", base, exc_info=True)
    with _LOCK:
        _PROPS[base] = (now + ttl, fingerprint)
    return fingerprint


# ── the index of saved files ───────────────────────────────────────────────

def _index_path(directory: str) -> str:
    return os.path.join(directory, INDEX_NAME)


def _load_index(directory: str) -> Dict[str, Any]:
    try:
        with open(_index_path(directory), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "entries": {}}


def _write_index(directory: str, index: Dict[str, Any]) -> None:
    os.makedirs(directory, exist_ok=True)
    tmp = _index_path(directory) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _index_path(directory))


def _unlink(directory: str, filename: str) -> int:
    path = os.path.join(directory, filename)
    try:
        size = os.path.getsize(path)
        os.remove(path)
        return size
    except OSError:
        return 0


def _entry_for(key: str, base: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        entry = _load_index(cache_dir())["entries"].get(key)
    if isinstance(entry, dict) and base in (entry.get("servers") or []):
        return entry
    return None


def _drop_entry(key: str) -> None:
    directory = cache_dir()
    with _LOCK:
        index = _load_index(directory)
        entry = index["entries"].pop(key, None)
        if entry:
            _unlink(directory, str(entry.get("file") or filename_for(key)))
            try:
                _write_index(directory, index)
            except OSError:
                logger.debug("slot cache: index not written", exc_info=True)


def _record_entry(key: str, base: str, session_id: str, model: str, info: Dict[str, Any]) -> None:
    directory = cache_dir()
    now = time.time()
    with _LOCK:
        index = _load_index(directory)
        entries = index["entries"]
        entry = entries.get(key) or {"created": now, "sessions": [], "servers": []}
        entry.update({"file": filename_for(key), "model": model, "last_used": now})
        for field_name in ("bytes", "tokens", "rate_tpms"):
            if info.get(field_name) is not None:
                entry[field_name] = info[field_name]
        if session_id and session_id not in entry["sessions"]:
            entry["sessions"].append(session_id)
        if base not in entry["servers"]:
            entry["servers"].append(base)
        entries[key] = entry
        # A chat whose document changed no longer needs the older state.
        if session_id:
            for other_key, other in list(entries.items()):
                if other_key == key:
                    continue
                sessions = [s for s in (other.get("sessions") or []) if s != session_id]
                if sessions != (other.get("sessions") or []):
                    if sessions:
                        other["sessions"] = sessions
                    else:
                        _unlink(directory, str(other.get("file") or filename_for(other_key)))
                        entries.pop(other_key, None)
        _write_index(directory, index)


def _touch(key: str, session_id: str) -> None:
    directory = cache_dir()
    with _LOCK:
        index = _load_index(directory)
        entry = index["entries"].get(key)
        if not entry:
            return
        entry["last_used"] = time.time()
        if session_id and session_id not in (entry.get("sessions") or []):
            entry.setdefault("sessions", []).append(session_id)
        try:
            _write_index(directory, index)
        except OSError:
            logger.debug("slot cache: index not written", exc_info=True)


def cleanup() -> Dict[str, int]:
    """Drop entries whose file is gone, older than the age cap, then the least
    recently used until the folder fits the size cap; delete orphan files."""
    directory = cache_dir()
    removed = freed = 0
    with _LOCK:
        if not os.path.isdir(directory):
            return {"removed": 0, "freed_bytes": 0}
        index = _load_index(directory)
        entries = index["entries"]
        now = time.time()
        for key, entry in list(entries.items()):
            filename = str(entry.get("file") or filename_for(key))
            path = os.path.join(directory, filename)
            if not os.path.exists(path):
                entries.pop(key, None)
                removed += 1
                continue
            if now - float(entry.get("last_used") or entry.get("created") or now) > max_age_s():
                freed += _unlink(directory, filename)
                entries.pop(key, None)
                removed += 1
        sizes = {}
        for key, entry in entries.items():
            try:
                sizes[key] = os.path.getsize(os.path.join(directory, str(entry.get("file") or filename_for(key))))
            except OSError:
                sizes[key] = int(entry.get("bytes") or 0)
        total = sum(sizes.values())
        for key in sorted(entries, key=lambda k: float(entries[k].get("last_used") or 0)):
            if total <= max_bytes():
                break
            freed += _unlink(directory, str(entries[key].get("file") or filename_for(key)))
            total -= sizes.get(key, 0)
            entries.pop(key, None)
            removed += 1
        known = {str(e.get("file") or filename_for(k)) for k, e in entries.items()}
        try:
            names = os.listdir(directory)
        except OSError:
            names = []
        for name in names:
            if name.startswith(FILE_PREFIX) and name.endswith(FILE_SUFFIX) and name not in known:
                path = os.path.join(directory, name)
                try:
                    if now - os.path.getmtime(path) > 3600:
                        freed += _unlink(directory, name)
                        removed += 1
                except OSError:
                    pass
        try:
            _write_index(directory, index)
        except OSError:
            logger.debug("slot cache: index not written", exc_info=True)
    return {"removed": removed, "freed_bytes": freed}


def status() -> Dict[str, Any]:
    directory = cache_dir()
    with _LOCK:
        entries = _load_index(directory)["entries"] if os.path.isdir(directory) else {}
        now = time.monotonic()
        rows = []
        total = 0
        for key, entry in sorted(entries.items(), key=lambda kv: -float(kv[1].get("last_used") or 0)):
            size = int(entry.get("bytes") or 0)
            total += size
            rows.append({
                "key": key[:12], "file": entry.get("file"), "bytes": size,
                "tokens": entry.get("tokens"), "model": entry.get("model"),
                "sessions": len(entry.get("sessions") or []),
                "servers": list(entry.get("servers") or []),
                "created": entry.get("created"), "last_used": entry.get("last_used"),
            })
        return {
            "enabled": enabled(), "dir": directory,
            "max_tokens": max_tokens(), "max_mb": max_bytes() // (1024 * 1024),
            "max_age_days": int(max_age_s() // 86400),
            "entries": rows, "total_bytes": total,
            "unsupported_servers": sorted(b for b, until in _UNSUPPORTED.items() if until > now),
            "stats": dict(_STATS),
        }


def clear(session_id: Optional[str] = None) -> Dict[str, int]:
    """Delete saved states: every one, or those only this chat uses (a state
    shared with another chat just stops listing this one)."""
    directory = cache_dir()
    removed = freed = 0
    with _LOCK:
        index = _load_index(directory) if os.path.isdir(directory) else {"entries": {}}
        entries = index["entries"]
        for key, entry in list(entries.items()):
            if session_id:
                sessions = [s for s in (entry.get("sessions") or []) if s != session_id]
                if sessions == (entry.get("sessions") or []):
                    continue
                if sessions:
                    entry["sessions"] = sessions
                    continue
            freed += _unlink(directory, str(entry.get("file") or filename_for(key)))
            entries.pop(key, None)
            removed += 1
        if os.path.isdir(directory):
            if not session_id:
                for name in os.listdir(directory):
                    if name.startswith(FILE_PREFIX) and name.endswith(FILE_SUFFIX):
                        freed += _unlink(directory, name)
            try:
                _write_index(directory, index)
            except OSError:
                logger.debug("slot cache: index not written", exc_info=True)
        # The slots may still hold a state: ask for a restore next time.
        if not session_id:
            _STATE.clear()
        else:
            for slot_key, state in list(_STATE.items()):
                if state and state.get("session") == session_id:
                    _STATE[slot_key] = None
    return {"removed": removed, "freed_bytes": freed}


# ── a turn ──────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    base: str
    slot: int
    session_id: str
    model: str
    headers: Optional[Dict[str, Any]]
    status: str = "off"
    reason: str = ""
    key: str = ""
    est_tokens: int = 0
    will_save: bool = False
    restore_ms: Optional[float] = None
    done: bool = False
    report: Dict[str, Any] = field(default_factory=dict)


def _forget_slot(base: str, slot: int) -> None:
    with _LOCK:
        _STATE[(base, slot)] = None


async def _await_pending(base: str, slot: int) -> None:
    with _LOCK:
        pending = _PENDING.get((base, slot))
    if pending is None or pending.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(pending), PENDING_WAIT_S)
    except Exception:  # noqa: BLE001 - a slow save never holds the turn
        logger.debug("slot cache: earlier save still running", exc_info=True)


async def prepare(url: str, model: str, payload: Dict[str, Any], messages: List[Dict[str, Any]],
                  tools: Any, session_id: Optional[str],
                  headers: Optional[Dict[str, Any]] = None) -> Optional[Plan]:
    """Before a request to a llama-server slot: restore the pinned document's
    state when the slot lost it. Returns None when the turn has nothing to do
    with this cache; never raises."""
    try:
        return await _prepare(url, model, payload, messages, tools, session_id, headers)
    except Exception:  # noqa: BLE001 - a turn never fails on this
        with _LOCK:
            _STATS["errors"] += 1
        logger.debug("slot cache: prepare failed", exc_info=True)
        return None


async def _prepare(url, model, payload, messages, tools, session_id, headers) -> Optional[Plan]:
    if not enabled() or not session_id:
        return None
    slot = payload.get("id_slot")
    if not isinstance(slot, int) or isinstance(slot, bool):
        return None
    base = server_base(url)
    sid = str(session_id)
    prefix, docs = pinned_prefix(messages)
    if not docs:
        _forget_slot(base, slot)  # this request puts something else in the slot
        return None
    plan = Plan(base=base, slot=slot, session_id=sid, model=str(model or ""), headers=headers)
    from src.model_context import estimate_tokens
    plan.est_tokens = estimate_tokens(prefix)
    with _LOCK:
        unsupported = _UNSUPPORTED.get(base, 0.0) > time.monotonic()
    if unsupported:
        plan.reason = "unsupported"
    elif plan.est_tokens > max_tokens():
        plan.reason = "too_large"
    elif plan.est_tokens < MIN_PREFIX_TOKENS:
        plan.reason = "too_small"
    if plan.reason:
        _forget_slot(base, slot)
        return plan

    await _await_pending(base, slot)
    fingerprint = await _server_fingerprint(base, headers)
    plan.key = prefix_key(model, fingerprint, prefix, tools)
    with _LOCK:
        held = _STATE.get((base, slot))
        _STATE[(base, slot)] = None  # being rewritten until this turn ends
    entry = _entry_for(plan.key, base)
    if held and held.get("key") == plan.key:
        plan.status = "warm"
        if entry:
            _touch(plan.key, sid)
    elif entry and os.path.exists(os.path.join(cache_dir(), str(entry.get("file") or filename_for(plan.key)))):
        plan.status = "restored" if await _restore(plan, entry) else "miss"
    else:
        plan.status = "miss"
    with _LOCK:
        failed_at = _SAVE_FAILED.get(plan.key, 0.0)
    plan.will_save = (entry is None or plan.status == "miss") and \
        time.monotonic() - failed_at > SAVE_FAIL_RETRY_S
    return plan


async def _restore(plan: Plan, entry: Dict[str, Any]) -> bool:
    started = time.monotonic()
    filename = str(entry.get("file") or filename_for(plan.key))
    try:
        resp = await _call("POST", f"{plan.base}/slots/{plan.slot}", params={"action": "restore"},
                           body={"filename": filename}, headers=plan.headers, timeout=RESTORE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        _log_once(f"restore-net:{plan.base}", "Slot cache: restore on %s failed (%s); reading the prompt normally",
                  plan.base, exc)
        return False
    if resp.status_code == 200:
        plan.restore_ms = round((time.monotonic() - started) * 1000.0, 1)
        _touch(plan.key, plan.session_id)
        return True
    if resp.status_code == 501:
        with _LOCK:
            _UNSUPPORTED[plan.base] = time.monotonic() + UNSUPPORTED_TTL
        plan.reason = "unsupported"
    else:
        # The file is not usable (other context size, damaged, removed).
        _drop_entry(plan.key)
    _log_once(f"restore:{plan.base}:{resp.status_code}",
              "Slot cache: %s answered HTTP %s to a restore; reading the prompt normally",
              plan.base, resp.status_code)
    return False


async def _save(plan: Plan, timings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    filename = filename_for(plan.key)
    try:
        resp = await _call("POST", f"{plan.base}/slots/{plan.slot}", params={"action": "save"},
                           body={"filename": filename}, headers=plan.headers, timeout=SAVE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _SAVE_FAILED[plan.key] = time.monotonic()
        _log_once(f"save-net:{plan.base}", "Slot cache: save on %s failed (%s)", plan.base, exc)
        return None
    if resp.status_code != 200:
        with _LOCK:
            _SAVE_FAILED[plan.key] = time.monotonic()
            if resp.status_code == 501:
                _UNSUPPORTED[plan.base] = time.monotonic() + UNSUPPORTED_TTL
                plan.reason = "unsupported"
        _log_once(f"save:{plan.base}:{resp.status_code}",
                  "Slot cache: %s answered HTTP %s to a save (is it started with --slot-save-path?)",
                  plan.base, resp.status_code)
        return None
    try:
        answer = resp.json()
    except ValueError:
        answer = {}
    answer = answer if isinstance(answer, dict) else {}
    rate = None
    prompt_n, prompt_ms = timings.get("prompt_n"), timings.get("prompt_ms")
    if isinstance(prompt_n, (int, float)) and isinstance(prompt_ms, (int, float)) and prompt_n >= 64 and prompt_ms > 0:
        rate = prompt_n / prompt_ms
    size = answer.get("n_written")
    if not isinstance(size, int):
        try:
            size = os.path.getsize(os.path.join(cache_dir(), filename))
        except OSError:
            size = None
    try:
        _record_entry(plan.key, plan.base, plan.session_id, plan.model,
                      {"bytes": size, "tokens": answer.get("n_saved"), "rate_tpms": rate})
        cleanup()
    except OSError as exc:
        _log_once("index", "Slot cache: the index in %s cannot be written (%s)", cache_dir(), exc)
        return None
    save_ms = None
    if isinstance(answer.get("timings"), dict):
        save_ms = answer["timings"].get("save_ms")
    return {"bytes": size, "save_ms": save_ms}


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


async def finish(plan: Optional[Plan], timings: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """When the answer is complete: the `prefix_cache` record for the turn's
    metrics (saving the slot first when this turn processed the prefix).
    Never raises."""
    if plan is None:
        return None
    if plan.done:
        return plan.report
    plan.done = True
    try:
        report = await _finish(plan, timings or {})
    except Exception:  # noqa: BLE001
        with _LOCK:
            _STATS["errors"] += 1
        logger.debug("slot cache: finish failed", exc_info=True)
        report = {"status": plan.status, "slot": plan.slot}
    plan.report = report
    with _LOCK:
        _STATS[report.get("status", "off")] = _STATS.get(report.get("status", "off"), 0) + 1
        if isinstance(report.get("prefill_saved_ms"), (int, float)):
            _STATS["prefill_saved_ms"] += float(report["prefill_saved_ms"])
    return report


async def _finish(plan: Plan, timings: Dict[str, Any]) -> Dict[str, Any]:
    status, reason = plan.status, plan.reason
    prompt_n, cache_n, prompt_ms = (_num(timings.get(k)) for k in ("prompt_n", "cache_n", "prompt_ms"))
    report: Dict[str, Any] = {"slot": plan.slot, "prefix_tokens": plan.est_tokens}
    if status == "off":
        report.update(status="off", reason=reason or "off")
        return report

    entry = _entry_for(plan.key, plan.base)
    if status in ("restored", "warm") and cache_n is not None and plan.est_tokens \
            and cache_n < REUSE_FLOOR * plan.est_tokens:
        # The server says it read the prefix again: the state was gone or
        # could not be trimmed. Treat it as a miss and save it afresh.
        was, status, reason = status, "miss", "the server did not reuse the saved prefix"
        if was == "restored":
            # The file did not work: drop it and save a fresh one.
            _drop_entry(plan.key)
            entry = None
            plan.will_save = True
        else:
            # The slot was emptied behind our back (a server restart): the
            # file is still good, and the slot holds the prefix again now.
            plan.will_save = entry is None
        _log_once(f"noreuse:{plan.base}", "Slot cache: %s read the pinned document again after a %s; "
                  "the saved state is not reusable there", plan.base, plan.status)

    if status in ("restored", "warm") and entry:
        rate = _num(entry.get("rate_tpms"))
        if cache_n and rate:
            report["prefill_saved_ms"] = round(cache_n / rate, 1)

    pending = False
    save_ms = None
    if plan.will_save and status in ("miss", "warm"):
        task = asyncio.ensure_future(_save(plan, timings))
        with _LOCK:
            _PENDING[(plan.base, plan.slot)] = task
        try:
            result = await asyncio.wait_for(asyncio.shield(task), SAVE_BUDGET_S)
        except asyncio.TimeoutError:
            pending, result = True, {}
        except Exception:  # noqa: BLE001
            result = None
        if result is not None:
            status = "saved"
            save_ms = result.get("save_ms")
            if result.get("bytes"):
                report["bytes"] = result["bytes"]
            if pending:
                report["pending"] = True
        elif plan.reason == "unsupported":
            status = "off"

    with _LOCK:
        _STATE[(plan.base, plan.slot)] = {"key": plan.key, "session": plan.session_id, "at": time.time()}
    if plan.reason == "unsupported" and status == "off":
        reason = "unsupported"
    report["status"] = status
    if reason:
        report["reason"] = reason
    for name, value in (("restore_ms", plan.restore_ms), ("save_ms", save_ms), ("prompt_n", prompt_n),
                        ("cache_n", cache_n), ("prompt_ms", prompt_ms)):
        if value is not None:
            report[name] = value if name == "restore_ms" else (int(value) if name.endswith("_n") else value)
    return report


def merge_reports(current: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Fold a round's record into a turn's: the most telling status wins and
    the saved prefill time adds up."""
    if not isinstance(new, dict) or not new.get("status"):
        return current
    if not isinstance(current, dict) or not current.get("status"):
        return dict(new)
    better = STATUS_PRIORITY.get(new["status"], 0) > STATUS_PRIORITY.get(current["status"], 0)
    merged = dict(new if better else current)
    saved = [r.get("prefill_saved_ms") for r in (current, new) if isinstance(r.get("prefill_saved_ms"), (int, float))]
    if saved:
        merged["prefill_saved_ms"] = round(sum(saved), 1)
    merged["rounds"] = int(current.get("rounds") or 1) + 1
    return merged


async def drain() -> None:
    """Wait for saves still running in the background (tests, shutdown)."""
    with _LOCK:
        tasks = [t for t in _PENDING.values() if not t.done()]
    for task in tasks:
        try:
            await asyncio.wait_for(asyncio.shield(task), SAVE_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            pass
