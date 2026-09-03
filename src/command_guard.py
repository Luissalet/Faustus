"""Destructive command guard (dcg + slb mechanics).

Classifies every shell-ish command the agent wants to run (``bash`` content,
``python`` tool source) into SAFE | CAUTION | DANGEROUS | CRITICAL, so the
EXISTING sealed exact-approval flow (src/tool_approvals.py + the card flow in
src/agent_loop.py) can gate the destructive tiers. This module never approves
anything itself — it only classifies, records, and explains.

Design (dcg):
  1. whitelist-first — known-safe full commands return SAFE immediately;
  2. fast substring reject — no danger substring, no regex work;
  3. regex packs (fs / git / db / containers / system), highest tier wins;
  4. inline/heredoc scanning — ``bash -c "..."``, ``python -c '...'``,
     heredoc bodies and python string literals are classified too;
  5. fail-open latency budget — if the wall clock runs out, return the highest
     tier found SO FAR with ``fail_open=True``. classify() NEVER raises.

Three release valves (dcg), in escalating ceremony:
  - allowlist entries in DATA_DIR/command_guard.json (exact or prefix, TTL),
  - a one-shot env bypass (FAUSTUS_GUARD_ALLOW_ONCE = sha256 of the command),
  - the sealed per-command approval card (handled by the caller).

Decision receipts (franken_engine, light): DATA_DIR/command_guard_log.jsonl,
hash-chained so tampering is detectable; ``verify_chain`` audits it.

Pure stdlib. Nothing in here may raise into the tool-execution hot path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Module-level so tests can point the store/log somewhere disposable.
DATA_DIR = _DEFAULT_DATA_DIR

# Monkeypatchable clock (tests drive the fail-open budget with it).
_now = time.perf_counter

TIERS = ("SAFE", "CAUTION", "DANGEROUS", "CRITICAL")
_TIER_RANK = {name: rank for rank, name in enumerate(TIERS)}

GUARD_MODES = ("off", "observe", "enforce")
DEFAULT_GUARD_MODE = "enforce"

ONE_SHOT_ENV = "FAUSTUS_GUARD_ALLOW_ONCE"

_MAX_LOG_BYTES = 2 * 1024 * 1024
_GENESIS_HASH = "0" * 64
_COMMAND_HEAD_CHARS = 160
_MAX_INLINE_DEPTH = 2


@dataclass
class GuardDecision:
    """The classifier's verdict for one command. Deterministic, pure."""

    tier: str
    rule: str = ""
    pack: str = ""
    matched: str = ""
    trace: list[str] = field(default_factory=list)
    fail_open: bool = False

    @property
    def rule_id(self) -> str:
        return f"{self.pack}.{self.rule}" if self.pack else (self.rule or "")


def tier_at_least(tier: str, floor: str) -> bool:
    return _TIER_RANK.get(tier, 0) >= _TIER_RANK.get(floor, 0)


# ---------------------------------------------------------------------------
# Stage 1 — whitelist-first
# ---------------------------------------------------------------------------
# Anchored on the WHOLE command and refusing shell chaining metacharacters, so
# ``git status && rm -rf /`` can never ride the ``git status`` pattern.

_WL_TAIL = r"[^;&|<>`$\n]*"

_WHITELIST: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(r"(?i)\s*" + pattern + r"\s*\Z"))
    for name, pattern in (
        ("git_read", r"git\s+(?:status|log|diff|show|fetch|shortlog|describe|blame|remote(?:\s+-v)?)\b" + _WL_TAIL),
        ("git_branch_list", r"git\s+branch(?:\s+(?:-[av]{1,2}|--list|--all|--show-current|--merged|--contains\s+\S+))*"),
        ("list_dir", r"(?:ls|dir|pwd|whoami|hostname|date|uptime|env|printenv|id|df|du|tree)\b" + _WL_TAIL),
        ("read_file", r"(?:cat|type|head|tail|less|more|wc|file|stat|md5sum|sha256sum)\b" + _WL_TAIL),
        ("search", r"(?:grep|rg|egrep|fgrep|findstr|ag)\b" + _WL_TAIL),
        ("find_no_delete", r"find\b(?![^\n]*-delete)" + _WL_TAIL),
        ("pytest", r"(?:python[\d.]*\s+-m\s+)?pytest\b" + _WL_TAIL),
        ("pip_read", r"(?:pip[\d.]*|uv\s+pip)\s+(?:list|show|freeze|check)\b" + _WL_TAIL),
        ("node_check", r"node\s+--check\b" + _WL_TAIL),
        ("npm_read", r"npm\s+(?:test|ls|view|outdated|ping)\b" + _WL_TAIL),
        ("echo", r"echo\b" + _WL_TAIL),
        ("which", r"(?:which|where|command\s+-v)\s+" + _WL_TAIL),
        ("proc_read", r"(?:ps|free|nvidia-smi|uname|top\s+-b\S*)\b" + _WL_TAIL),
        ("net_read", r"(?:curl|wget)\b(?![^\n]*\s(?:-o|--output)\s*(?:/dev/|\\\\\.\\))" + _WL_TAIL),
    )
)


# ---------------------------------------------------------------------------
# Stage 2 — fast substring reject
# ---------------------------------------------------------------------------
# If the lowercased command contains NONE of these, no regex ever runs.
# Over-inclusion is free (we just do the regex work); under-inclusion would be
# a hole, so the list errs long.

_DANGER_SUBSTRINGS = (
    "rm", "rmdir", "del ", "remove-item", "rd ", "unlink", "rmtree",
    "reset", "clean", "force", "-f", "/f", "drop", "truncate", "delete",
    "format", "mkfs", "dd ", "shutdown", "reboot", "halt", "poweroff",
    "kill", "prune", "purge", ">", "mv ", "git push", "checkout", "restore",
    "stash", "filter-branch", "filter-repo", "update-ref", "branch -d",
    "chmod", "chown", "reg ", "reg.exe", ":(){", "docker", "kubectl",
    "stop-computer", "restart-computer", "set-executionpolicy", "taskkill",
    "os.remove", "os.unlink", "subprocess", "os.system", "<<",
)


# ---------------------------------------------------------------------------
# Stage 3 — regex packs
# ---------------------------------------------------------------------------
# Each rule: (name, tier, compiled pattern). Recall > precision — the consumer
# of a DANGEROUS/CRITICAL verdict is a human approval card, not an auto-block.

def _rule(name: str, tier: str, pattern: str, flags: int = re.IGNORECASE) -> tuple[str, str, re.Pattern]:
    return name, tier, re.compile(pattern, flags)


# Root-ish rm targets: /, /*, ~, .., a bare drive root, or a top-level system
# directory. Kept syntactic on purpose — no filesystem I/O in the classifier.
_ROOTISH = (
    r"(?:/(?:\*|(?:etc|usr|bin|sbin|boot|dev|home|lib(?:64)?|opt|proc|root|run|srv|sys|var|windows)\b/?\*?)?"
    r"|~(?:/\*?)?"
    r"|\.\.(?:[\\/]\*?)?"
    r"|[a-z]:(?:[\\/]\*?)?)"
)

_PACKS: dict[str, tuple[tuple[str, str, re.Pattern], ...]] = {
    "fs": (
        _rule(
            "rm_root",
            "CRITICAL",
            r"\brm(?=\s)(?=[^\n]*\s-{1,2}[a-z]*r|[^\n]*--recursive)"
            r"(?=[^\n]*\s-{1,2}[a-z]*f|[^\n]*--force)"
            r"[^\n]*?(?:\s|=)([\"']?)" + _ROOTISH + r"\1(?=[\s;|&\"']|$)",
        ),
        _rule(
            "rm_force_recursive",
            "DANGEROUS",
            r"\brm(?=\s)(?=[^\n]*\s-{1,2}[a-z]*r|[^\n]*--recursive)"
            r"(?=[^\n]*\s-{1,2}[a-z]*f|[^\n]*--force)",
        ),
        _rule("rm_recursive", "DANGEROUS", r"\brm(?=\s)(?=[^\n]*\s-[a-z]*r\b|[^\n]*--recursive\b)"),
        _rule("rm_plain", "CAUTION", r"\brm\s+\S"),
        _rule("rmdir_subtree", "DANGEROUS", r"\brmdir\b[^\n]*(?:\s/s\b|\s--?p\b)"),
        _rule("rmdir_plain", "CAUTION", r"\brmdir\s+\S"),
        _rule("del_force", "DANGEROUS", r"(?:^|[\s;&|(])del\b[^\n]*\s/(?:f|s|q)\b"),
        _rule("del_plain", "CAUTION", r"(?:^|[\s;&|(])del\s+\S"),
        _rule("rd_subtree", "DANGEROUS", r"(?:^|[\s;&|(])rd\b[^\n]*\s/s\b"),
        _rule(
            "remove_item_recurse_force",
            "DANGEROUS",
            r"\bremove-item\b(?=[^\n]*-recurse)(?=[^\n]*-force)",
        ),
        _rule("remove_item", "CAUTION", r"\bremove-item\s+\S"),
        _rule("rmtree", "DANGEROUS", r"\b(?:shutil\s*\.\s*)?rmtree\s*\("),
        _rule("os_remove", "CAUTION", r"\bos\s*\.\s*(?:remove|unlink)\s*\("),
        _rule("find_delete", "DANGEROUS", r"\bfind\b[^\n]*\s-delete\b"),
        _rule("mv_devnull", "DANGEROUS", r"\bmv\b[^\n]*\s/dev/null"),
    ),
    "git": (
        _rule("reset_hard", "DANGEROUS", r"\bgit\b[^\n]*\breset\b[^\n]*--hard\b"),
        _rule("clean_force", "DANGEROUS", r"\bgit\b[^\n]*\bclean\b[^\n]*\s-[a-z]*f"),
        _rule(
            # dcg's canonical example: --force-with-lease is the SAFE spelling
            # and must not trip the force rule.
            "push_force",
            "DANGEROUS",
            r"\bgit\b[^\n]*\bpush\b(?=[^\n]*(?:--force\b(?!-with-lease)|\s-f\b))",
        ),
        # -D (force delete) is meaningfully different from -d, so no (?i).
        _rule("branch_force_delete", "CAUTION", r"\bgit\b[^\n]*\bbranch\b[^\n]*\s-D\b", 0),
        _rule("checkout_discard", "CAUTION", r"\bgit\b[^\n]*\b(?:checkout\s+(?:[^\n]*\s)?--|restore)\s+\.(?:\s|$)"),
        _rule("stash_drop", "CAUTION", r"\bgit\b[^\n]*\bstash\s+(?:drop|clear)\b"),
        _rule("filter_branch", "DANGEROUS", r"\bgit\b[^\n]*\bfilter-(?:branch|repo)\b|\bgit-filter-repo\b"),
        _rule("update_ref_delete", "DANGEROUS", r"\bgit\b[^\n]*\bupdate-ref\b[^\n]*\s-d\b"),
    ),
    "db": (
        _rule("drop_object", "DANGEROUS", r"\bdrop\s+(?:table|database|schema)\b"),
        _rule("truncate", "DANGEROUS", r"\btruncate\s+(?:table\s+)?[\"'`\[]?\w"),
        _rule(
            # DELETE FROM with no WHERE on the same statement (to ; or EOL).
            "delete_without_where",
            "DANGEROUS",
            r"\bdelete\s+from\s+\S+(?![^;\n]*\bwhere\b)",
        ),
        _rule("delete_with_where", "CAUTION", r"\bdelete\s+from\s+\S+[^;\n]*\bwhere\b"),
    ),
    "containers": (
        _rule(
            "docker_prune_all_volumes",
            "DANGEROUS",
            r"\bdocker\s+(?:system|image|volume|container|network)\s+prune\b"
            r"(?=[^\n]*(?:\s-[a-z]*a\b|--all\b))(?=[^\n]*--volumes\b)",
        ),
        _rule("docker_prune", "CAUTION", r"\bdocker\s+(?:system|image|volume|container|network)\s+prune\b"),
        _rule("docker_rm_force", "CAUTION", r"\bdocker\s+(?:container\s+)?rm\b[^\n]*\s-[a-z]*f"),
        _rule(
            "kubectl_delete_all",
            "CRITICAL",
            r"\bkubectl\b[^\n]*\bdelete\b(?=[^\n]*(?:--all\b|\s-n\s|\s--namespace\b))",
        ),
        _rule("kubectl_delete", "DANGEROUS", r"\bkubectl\b[^\n]*\bdelete\b"),
        _rule(
            "compose_down_volumes",
            "DANGEROUS",
            r"\bdocker(?:-|\s+)compose\b[^\n]*\bdown\b[^\n]*(?:\s-[a-z]*v\b|--volumes\b)",
        ),
    ),
    "system": (
        _rule("mkfs", "CRITICAL", r"\bmkfs(?:\.\w+)?\b"),
        _rule("dd_device", "CRITICAL", r"\bdd\b[^\n]*\bof=(?:/dev/|\\\\\.\\)"),
        _rule("format_drive", "CRITICAL", r"(?:^|[\s;&|(])format(?:\.com)?\s+[a-z]:"),
        _rule("fork_bomb", "CRITICAL", r":\(\)\s*\{", 0),
        _rule("shutdown", "DANGEROUS", r"(?:^|[\s;&|(])(?:shutdown|reboot|halt|poweroff)\b"),
        _rule("stop_computer", "DANGEROUS", r"\b(?:stop|restart)-computer\b"),
        _rule("chmod_777_root", "DANGEROUS", r"\bchmod\b(?=[^\n]*\s-[a-zA-Z]*R)(?=[^\n]*\b777\b)[^\n]*\s/(?:\s|$|\*)"),
        _rule("chmod_recursive", "CAUTION", r"\bchmod\s+-[a-zA-Z]*R\b|\bchown\s+-[a-zA-Z]*R\b"),
        _rule("reg_delete", "DANGEROUS", r"\breg(?:\.exe)?\s+delete\b"),
        _rule("set_execution_policy", "CAUTION", r"\bset-executionpolicy\b"),
        _rule("taskkill_force", "CAUTION", r"\btaskkill\b[^\n]*/f\b"),
        _rule("kill_9", "CAUTION", r"\b(?:kill|pkill)\s+(?:-9|-KILL|-s\s+KILL)\b"),
    ),
}

ALL_PACKS = frozenset(_PACKS)


def packs_from_setting(raw: Any) -> frozenset[str]:
    """Parse the ``agent_command_guard_packs`` setting ("all" or a CSV)."""
    text = str(raw or "").strip().lower()
    if not text or text == "all":
        return ALL_PACKS
    picked = {part.strip() for part in text.split(",") if part.strip()}
    known = picked & ALL_PACKS
    return frozenset(known) if known else ALL_PACKS


# ---------------------------------------------------------------------------
# Stage 4 — inline / heredoc scanning
# ---------------------------------------------------------------------------

_INLINE_BODY_RE = re.compile(
    r"(?i)\b(?:python[\w.]*|bash|sh|zsh|dash|ksh|pwsh|powershell(?:\.exe)?|cmd(?:\.exe)?)"
    r"\s+(?:-c|-command|/c)\s+"
    r"(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)'|(\S+))"
)
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1\n(.*?)(?:\n\2\b|\Z)", re.S)
_PY_STRING_RE = re.compile(
    r"(?s)(?:'''(.*?)'''|\"\"\"(.*?)\"\"\"|'([^'\n]*)'|\"([^\"\n]*)\")"
)


def _inline_bodies(command: str) -> list[tuple[str, str]]:
    bodies: list[tuple[str, str]] = []
    for match in _INLINE_BODY_RE.finditer(command):
        body = next((g for g in match.groups() if g), "")
        if body:
            bodies.append(("inline", body))
    for match in _HEREDOC_RE.finditer(command):
        if match.group(3):
            bodies.append(("heredoc", match.group(3)))
    return bodies


def _string_literals(source: str) -> list[str]:
    out: list[str] = []
    for match in _PY_STRING_RE.finditer(source):
        literal = next((g for g in match.groups() if g), "")
        if literal and len(literal) >= 2:
            out.append(literal)
    return out


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------

class _Budget:
    """Wall-clock guard for stages 3–4. Cheap to poll, monkeypatch-friendly."""

    def __init__(self, budget_ms: float):
        self.deadline = _now() + max(1.0, float(budget_ms)) / 1000.0
        self.exceeded = False

    def out_of_time(self) -> bool:
        if not self.exceeded and _now() > self.deadline:
            self.exceeded = True
        return self.exceeded


def _scan_packs(
    text: str,
    packs: Iterable[str],
    budget: _Budget,
    matches: list[dict],
    trace: list[str],
    stats: dict,
    origin: str,
) -> None:
    for pack_name in sorted(packs):
        rules = _PACKS.get(pack_name)
        if not rules:
            continue
        for rule_name, tier, pattern in rules:
            if budget.out_of_time():
                return
            stats["rules_tested"] += 1
            found = pattern.search(text)
            if found:
                snippet = found.group(0).strip()[:120]
                matches.append(
                    {"pack": pack_name, "rule": rule_name, "tier": tier,
                     "matched": snippet, "origin": origin}
                )
                trace.append(f"match {pack_name}.{rule_name} [{tier}] {origin}: {snippet!r}")


def _scan_command(
    text: str,
    packs: Iterable[str],
    budget: _Budget,
    matches: list[dict],
    trace: list[str],
    stats: dict,
    origin: str,
    depth: int,
    python_source: bool,
) -> None:
    lowered = text.lower()
    if not any(s in lowered for s in _DANGER_SUBSTRINGS):
        trace.append(f"substring-reject {origin}: no danger substrings")
        return
    _scan_packs(text, packs, budget, matches, trace, stats, origin)
    if depth >= _MAX_INLINE_DEPTH or budget.out_of_time():
        return
    for kind, body in _inline_bodies(text):
        _scan_command(body, packs, budget, matches, trace, stats,
                      f"{origin}>{kind}", depth + 1, python_source=True)
    if python_source:
        for literal in _string_literals(text):
            if budget.out_of_time():
                return
            lowered_lit = literal.lower()
            if any(s in lowered_lit for s in _DANGER_SUBSTRINGS):
                _scan_packs(literal, packs, budget, matches, trace, stats,
                            f"{origin}>str")


def _classify_impl(
    command: str,
    packs: Optional[Iterable[str]],
    budget_ms: float,
    python_source: bool,
) -> tuple[GuardDecision, dict]:
    text = command if isinstance(command, str) else ("" if command is None else str(command))
    active_packs = frozenset(packs) & ALL_PACKS if packs else ALL_PACKS
    if packs and not active_packs:
        active_packs = ALL_PACKS
    trace: list[str] = []
    stats = {"rules_tested": 0, "whitelist_tested": 0}

    if not text.strip():
        return GuardDecision("SAFE", trace=["empty command"]), stats

    # 1. whitelist-first (whole-command, chaining metacharacters refused).
    if not python_source:
        for name, pattern in _WHITELIST:
            stats["whitelist_tested"] += 1
            if pattern.fullmatch(text):
                trace.append(f"whitelist: {name}")
                return GuardDecision("SAFE", rule=name, pack="whitelist", trace=trace), stats

    # 2–4. substring reject, regex packs, inline/heredoc scanning — under the
    # fail-open budget.
    budget = _Budget(budget_ms)
    matches: list[dict] = []
    _scan_command(text, active_packs, budget, matches, trace, stats,
                  origin="top", depth=0, python_source=python_source)

    fail_open = budget.exceeded
    if fail_open:
        trace.append(
            f"budget exceeded after {stats['rules_tested']} rules — "
            "fail-open with the highest tier found so far"
        )
    if not matches:
        return GuardDecision("SAFE", trace=trace, fail_open=fail_open), stats
    winner = max(matches, key=lambda m: _TIER_RANK[m["tier"]])
    decision = GuardDecision(
        tier=winner["tier"],
        rule=winner["rule"],
        pack=winner["pack"],
        matched=winner["matched"],
        trace=trace,
        fail_open=fail_open,
    )
    return decision, stats


def classify(
    command: str,
    *,
    packs: Optional[Iterable[str]] = None,
    budget_ms: float = 50.0,
    python_source: bool = False,
) -> GuardDecision:
    """Classify one command. Deterministic, pure, no I/O — and NEVER raises:
    an internal bug fails open to SAFE with the error in the trace."""
    try:
        decision, _stats = _classify_impl(command, packs, budget_ms, python_source)
        return decision
    except Exception as exc:  # noqa: BLE001 - the hot path must survive us
        logger.warning("command_guard.classify failed open: %r", exc)
        return GuardDecision(
            "SAFE",
            trace=[f"internal error, fail-open: {exc!r}"],
            fail_open=True,
        )


def classify_tool(
    tool_name: Any,
    content: Any,
    *,
    packs: Optional[Iterable[str]] = None,
    budget_ms: float = 50.0,
) -> GuardDecision:
    """Classify a tool call's content. ``python`` tool content IS python
    source (string literals scanned too); everything else is shell."""
    command = content if isinstance(content, str) else ("" if content is None else str(content))
    return classify(
        command,
        packs=packs,
        budget_ms=budget_ms,
        python_source=(tool_name == "python"),
    )


def explain(command: str, packs: Optional[Iterable[str]] = None) -> dict:
    """Full classification trace for humans and coordinators. Never raises."""
    try:
        decision, stats = _classify_impl(command, packs, budget_ms=500.0,
                                         python_source=False)
    except Exception as exc:  # noqa: BLE001
        return {
            "command_head": str(command or "")[:_COMMAND_HEAD_CHARS],
            "tier": "SAFE", "rule": "", "pack": "", "matched": "",
            "fail_open": True, "trace": [f"internal error: {exc!r}"],
            "rules_tested": 0, "whitelist_tested": 0,
        }
    return {
        "command_head": str(command or "")[:_COMMAND_HEAD_CHARS],
        "tier": decision.tier,
        "rule": decision.rule,
        "pack": decision.pack,
        "rule_id": decision.rule_id,
        "matched": decision.matched,
        "fail_open": decision.fail_open,
        "trace": decision.trace,
        "rules_tested": stats["rules_tested"],
        "whitelist_tested": stats["whitelist_tested"],
    }


# ---------------------------------------------------------------------------
# Allowlist + one-shot bypass
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()


def _store_path() -> str:
    return os.path.join(DATA_DIR, "command_guard.json")


def _log_path() -> str:
    return os.path.join(DATA_DIR, "command_guard_log.jsonl")


def normalize_command(command: Any) -> str:
    return " ".join(str(command or "").split())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _entry_expired(entry: dict, now: Optional[datetime] = None) -> bool:
    raw = entry.get("expires_at")
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(str(raw))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True  # unparseable expiry: treat as expired, never as forever
    return (now or _utcnow()) >= expires


def _load_store_locked() -> dict:
    path = _store_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("allow"), list):
            raise ValueError("wrong shape")
        return data
    except FileNotFoundError:
        return {"allow": []}
    except (ValueError, OSError):
        try:
            os.replace(path, path + ".corrupt")
            logger.warning("command_guard.json was corrupt; moved to .corrupt")
        except OSError:
            pass
        return {"allow": []}


def _save_store_locked(data: dict) -> None:
    now = _utcnow()
    data = {
        "allow": [
            entry for entry in data.get("allow", [])
            if isinstance(entry, dict) and not _entry_expired(entry, now)
        ]
    }
    path = _store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".command_guard.", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_allowlist() -> list[dict]:
    with _store_lock:
        data = _load_store_locked()
    now = _utcnow()
    return [
        dict(entry) for entry in data.get("allow", [])
        if isinstance(entry, dict) and not _entry_expired(entry, now)
    ]


def add_allowlist_entry(
    pattern: str,
    *,
    kind: str = "exact",
    reason: str = "",
    added_by: str = "",
    ttl_hours: Optional[float] = None,
) -> dict:
    kind = str(kind or "exact").strip().lower()
    if kind not in ("exact", "prefix"):
        raise ValueError("kind must be 'exact' or 'prefix'")
    normalized = normalize_command(pattern)
    if not normalized:
        raise ValueError("pattern must not be empty")
    now = _utcnow()
    entry = {
        "pattern": normalized,
        "kind": kind,
        "reason": str(reason or ""),
        "added_by": str(added_by or ""),
        "created_at": now.isoformat(),
        "expires_at": (
            (now + timedelta(hours=float(ttl_hours))).isoformat()
            if ttl_hours else None
        ),
    }
    with _store_lock:
        data = _load_store_locked()
        data["allow"] = [
            e for e in data.get("allow", [])
            if not (isinstance(e, dict) and e.get("pattern") == normalized and e.get("kind") == kind)
        ]
        data["allow"].append(entry)
        _save_store_locked(data)
    return entry


def remove_allowlist_entry(pattern: Any = None, index: Any = None) -> bool:
    with _store_lock:
        data = _load_store_locked()
        entries = [e for e in data.get("allow", []) if isinstance(e, dict)]
        before = len(entries)
        if index is not None:
            try:
                idx = int(index)
                if 0 <= idx < len(entries):
                    entries.pop(idx)
            except (TypeError, ValueError):
                pass
        elif pattern is not None:
            normalized = normalize_command(pattern)
            entries = [e for e in entries if e.get("pattern") != normalized]
        removed = len(entries) < before
        if removed:
            data["allow"] = entries
            _save_store_locked(data)
    return removed


def is_allowlisted(command: Any) -> Optional[dict]:
    """The first live allowlist entry covering ``command``, or None.
    Exact matches on whitespace-normalized text; prefix on the same."""
    normalized = normalize_command(command)
    if not normalized:
        return None
    now = _utcnow()
    with _store_lock:
        data = _load_store_locked()
    for entry in data.get("allow", []):
        if not isinstance(entry, dict) or _entry_expired(entry, now):
            continue
        pattern = normalize_command(entry.get("pattern"))
        if not pattern:
            continue
        kind = str(entry.get("kind") or "exact")
        if kind == "exact" and normalized == pattern:
            return dict(entry)
        if kind == "prefix" and normalized.startswith(pattern):
            return dict(entry)
    return None


# One-shot operator bypass: FAUSTUS_GUARD_ALLOW_ONCE holds the SHA-256 hex of
# the exact command; it matches once per process.
_one_shot_consumed: set[str] = set()
_one_shot_lock = threading.Lock()


def consume_one_shot(command: Any) -> bool:
    expected = (os.environ.get(ONE_SHOT_ENV) or "").strip().lower()
    if not expected:
        return False
    digest = hashlib.sha256(
        str(command or "").encode("utf-8", "replace")
    ).hexdigest()
    if digest != expected:
        return False
    with _one_shot_lock:
        if digest in _one_shot_consumed:
            return False
        _one_shot_consumed.add(digest)
    return True


# ---------------------------------------------------------------------------
# Decision receipts (hash-chained JSONL)
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()
_last_hash_cache: dict[str, str] = {}


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _record_hash(prev_hash: str, record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256((prev_hash + _canonical(body)).encode("utf-8")).hexdigest()


def _tail_hash_locked(path: str) -> str:
    cached = _last_hash_cache.get(path)
    if cached:
        return cached
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return _GENESIS_HASH
    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            value = str(record.get("hash") or "")
            if value:
                return value
        except (ValueError, TypeError):
            continue
    return _GENESIS_HASH


def append_receipt(
    *,
    session: Any = "",
    tool: Any = "",
    command: Any = "",
    tier: str = "SAFE",
    rule: str = "",
    action: str = "allowed",
    note: str = "",
) -> Optional[dict]:
    """Append one hash-chained decision receipt. Never raises."""
    try:
        command_text = str(command or "")
        with _log_lock:
            path = _log_path()
            rotated = False
            try:
                if os.path.getsize(path) > _MAX_LOG_BYTES:
                    os.replace(path, path + ".1")  # the tail survives in .1
                    _last_hash_cache.pop(path, None)
                    rotated = True
            except OSError:
                pass
            prev_hash = _tail_hash_locked(path)
            record: dict[str, Any] = {
                "ts": _utcnow().isoformat(),
                "session": str(session or ""),
                "tool": str(tool or ""),
                "command_sha256": hashlib.sha256(
                    command_text.encode("utf-8", "replace")
                ).hexdigest(),
                "command_head": command_text[:_COMMAND_HEAD_CHARS],
                "tier": str(tier),
                "rule": str(rule or ""),
                "action": str(action),
                "prev_hash": prev_hash,
            }
            if note:
                record["note"] = str(note)[:400]
            if rotated:
                record["rotated_from"] = os.path.basename(path) + ".1"
            record["hash"] = _record_hash(prev_hash, record)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_canonical(record) + "\n")
            _last_hash_cache[path] = record["hash"]
            return record
    except Exception as exc:  # noqa: BLE001 - receipts must never break a turn
        logger.warning("command_guard receipt failed: %r", exc)
        return None


def tail_receipts(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 1000))
    try:
        with open(_log_path(), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                out.append(record)
        except (ValueError, TypeError):
            out.append({"corrupt_line": line[:200]})
    return out


def verify_chain(path: Optional[str] = None) -> dict:
    """Walk the receipts log and verify the hash chain."""
    path = path or _log_path()
    length = 0
    prev_hash = _GENESIS_HASH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return {"ok": True, "length": 0, "broken_at": None}
    for line_no, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            return {"ok": False, "length": length, "broken_at": line_no}
        if not isinstance(record, dict):
            return {"ok": False, "length": length, "broken_at": line_no}
        # A rotation starts a fresh chain; its first record says so.
        if length == 0 and record.get("rotated_from"):
            prev_hash = str(record.get("prev_hash") or _GENESIS_HASH)
        if (
            str(record.get("prev_hash") or "") != prev_hash
            or _record_hash(prev_hash, record) != str(record.get("hash") or "")
        ):
            return {"ok": False, "length": length, "broken_at": line_no}
        prev_hash = str(record["hash"])
        length += 1
    return {"ok": True, "length": length, "broken_at": None}


# ---------------------------------------------------------------------------
# The gate evaluation the enforcement layer (src/tool_capabilities.py) calls
# ---------------------------------------------------------------------------

def gate_check(
    tool_name: Any,
    content: Any,
    *,
    mode: str,
    packs: Optional[Iterable[str]] = None,
    session: Any = "",
) -> dict:
    """One complete guard evaluation for one shell call. Never raises.

    Returns {tier, rule, pack, rule_id, matched, fail_open, allowlisted,
    one_shot, denial}: ``denial`` is None to allow, or the reason string the
    approval card should carry.
    """
    safe = {
        "tier": "SAFE", "rule": "", "pack": "", "rule_id": "", "matched": "",
        "fail_open": False, "allowlisted": False, "one_shot": False,
        "denial": None,
    }
    try:
        mode = str(mode or DEFAULT_GUARD_MODE).strip().lower()
        if mode == "off":
            return safe
        command = content if isinstance(content, str) else ("" if content is None else str(content))
        decision = classify_tool(tool_name, command, packs=packs)
        result = {
            "tier": decision.tier,
            "rule": decision.rule,
            "pack": decision.pack,
            "rule_id": decision.rule_id,
            "matched": decision.matched,
            "fail_open": decision.fail_open,
            "allowlisted": False,
            "one_shot": False,
            "denial": None,
        }
        note = ""
        if tier_at_least(result["tier"], "DANGEROUS"):
            entry = None
            try:
                entry = is_allowlisted(command)
            except Exception:  # noqa: BLE001 - store trouble never blocks
                entry = None
            if entry is not None:
                result["allowlisted"] = True
                result["tier"] = "CAUTION"
                note = (
                    f"allowlisted ({entry.get('kind')}: {entry.get('pattern','')[:80]}) "
                    "— tier downgraded to CAUTION"
                )
                decision.trace.append(note)
            elif consume_one_shot(command):
                result["one_shot"] = True
                result["tier"] = "CAUTION"
                note = f"one-shot {ONE_SHOT_ENV} bypass consumed — tier downgraded to CAUTION"
                decision.trace.append(note)
        if result["tier"] == "SAFE":
            return result  # SAFE is not logged (volume)
        if mode == "observe":
            append_receipt(
                session=session, tool=tool_name, command=command,
                tier=result["tier"], rule=result["rule_id"],
                action="observed", note=note,
            )
            return result
        # enforce
        if result["tier"] == "CAUTION":
            append_receipt(
                session=session, tool=tool_name, command=command,
                tier=result["tier"], rule=result["rule_id"],
                action=("allowlisted" if (result["allowlisted"] or result["one_shot"]) else "allowed"),
                note=note,
            )
            return result
        append_receipt(
            session=session, tool=tool_name, command=command,
            tier=result["tier"], rule=result["rule_id"], action="blocked",
        )
        result["denial"] = (
            f"Destructive command (tier {result['tier']}, rule {result['rule_id']}): "
            f"{result['matched']!r} matched in this '{tool_name}' command. "
            "Destructive commands are confirmed separately "
            "(agent_command_guard_mode=enforce); approve this exact command to "
            "run it, or allowlist the pattern under /api/command-guard/allowlist."
        )
        return result
    except Exception as exc:  # noqa: BLE001 - fail open, never break the turn
        logger.warning("command_guard.gate_check failed open: %r", exc)
        safe["fail_open"] = True
        return safe
