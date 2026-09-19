"""src/tool_arg_policy.py — argument-level tool policy rules.

Name-level policy (src/tool_policy.py, src/agent_loop.py's `_denial_for_tool`)
can only say "this tool is fine" or "this tool is blocked" for every call to
it — there was no way to say "this tool is fine, but only with these
arguments" (web_fetch only to approved domains, a write tool only under a
given path prefix, an MCP connector only with a specific option). This module
adds that: a small set of per-argument rules, stored in the `tool_arg_rules`
setting, evaluated at the single point every tool call funnels through in
`src/agent_loop.py`'s per-block dispatch loop — right after the name-level
policy gate (`_denial_for_tool`) and before a call either executes or is
routed into the human approval flow.

Rule shape (one entry of the `tool_arg_rules` setting, a list; default []):

    {
        "id": "no-secrets-dir",
        "tool": "mcp__github__*",       # exact tool name, or a glob
        "arg": "options.path",           # dotted path into the arguments dict
        "op": "not_prefix",
        "value": "/etc",
        "action": "deny",                # "deny" (default) or "ask"
        "note": "shown to the model",
    }

Semantics
---------
Each rule is a POSITIVE constraint on one dotted argument path of one tool:
the op describes what the argument value MUST satisfy, and the rule
"violates" (fires, and its action applies) exactly when the value FAILS that
constraint.

    equals      value must equal rule.value exactly
    one_of      value must be one of rule.value (a list)
    prefix      str(value) must start with rule.value
    not_prefix  str(value) must NOT start with rule.value (a denylist prefix)
    regex       str(value) must fullmatch rule.value (a regex pattern)
    max_len     len(str(value)) must be <= rule.value (a number)
    domain_in   the URL/host in value must be, or be a subdomain of, one of
                rule.value (a list of domains)

A missing argument (the dotted path resolves to nothing) is a violation only
for the ops that require a specific value to be present — equals, one_of,
prefix, domain_in — since a rule requiring a particular value cannot be
satisfied by nothing. For not_prefix, regex and max_len, a missing argument
has nothing to violate, so those never fire on a missing arg.

`evaluate(tool_name, args)` returns the FIRST violated rule (rules are
checked in stored order), or None when nothing fires — an empty
`tool_arg_rules` setting is always a no-op, matching every tool call it saw
before this module existed.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

VALID_OPS = frozenset(
    {"equals", "one_of", "prefix", "not_prefix", "regex", "max_len", "domain_in"}
)
VALID_ACTIONS = frozenset({"deny", "ask"})

# Ops for which a missing argument counts as a violation — the rule requires
# a specific value to be present, so nothing at all cannot satisfy it.
_MISSING_ARG_VIOLATES = frozenset({"equals", "one_of", "prefix", "domain_in"})

# Timeout-safety bounds for the "regex" op (no true regex engine timeout in
# stdlib re, so the defense is bounding both sides): a rule's own pattern is
# capped at save time (`_validate_rule`), and an oversized argument value is
# simply not matched against it at evaluation time rather than risking a
# pathological match on attacker-controlled input.
MAX_REGEX_PATTERN_LEN = 200
MAX_REGEX_INPUT_LEN = 4096


class RuleError(ValueError):
    """A rule (or the rules list) this module refuses to store or run."""


_MISSING = object()


def _get_path(args: Any, dotted: str) -> Any:
    """Resolve a dotted path (`"options.path"`) into a nested dict. Returns
    the sentinel `_MISSING` when any segment is absent or the value at that
    point is not a dict — never raises."""
    cur = args
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _domain_of(value: Any) -> Optional[str]:
    """Best-effort hostname out of a URL or bare host string."""
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = raw if "//" in raw else f"//{raw}"
    try:
        host = urlparse(candidate).hostname
    except ValueError:
        return None
    return (host or "").lower() or None


def _domain_allowed(host: Optional[str], allowed: Any) -> bool:
    if not host or not isinstance(allowed, list):
        return False
    host = host.lower()
    for entry in allowed:
        domain = str(entry or "").strip().lower()
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _violates(op: str, actual: Any, rule_value: Any) -> bool:
    """True when `actual` FAILS the constraint `op` describes — i.e. the
    rule fires. Never raises: any op-specific failure to interpret the
    values (bad regex saved by hand-editing settings.json, non-numeric
    max_len, ...) is treated as "does not fire" rather than crashing
    evaluation for every other rule after it."""
    present = actual is not _MISSING
    try:
        if op == "equals":
            return True if not present else actual != rule_value
        if op == "one_of":
            if not present or not isinstance(rule_value, list):
                return not present
            return actual not in rule_value
        if op == "prefix":
            if not present:
                return True
            return not str(actual).startswith(str(rule_value))
        if op == "not_prefix":
            if not present:
                return False
            return str(actual).startswith(str(rule_value))
        if op == "regex":
            if not present:
                return False
            text = str(actual)
            if len(text) > MAX_REGEX_INPUT_LEN:
                return False
            return re.fullmatch(str(rule_value), text) is None
        if op == "max_len":
            if not present:
                return False
            limit = float(rule_value)
            return len(str(actual)) > limit
        if op == "domain_in":
            if not present:
                return True
            return not _domain_allowed(_domain_of(actual), rule_value)
    except Exception:  # noqa: BLE001 - never let one bad rule break evaluation
        return False
    return False


@dataclass(frozen=True)
class Decision:
    """A single violated rule, and what to do about it."""

    rule_id: str
    tool: str
    arg: str
    op: str
    value: Any
    action: str  # "deny" | "ask"
    note: str = ""

    def message(self) -> str:
        value_text = self.value if isinstance(self.value, str) else json.dumps(self.value)
        base = f"Blocked by policy rule {self.rule_id}: {self.arg} must {self.op} {value_text}."
        if self.note:
            base = f"{base} {self.note}"
        return base


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuleError(message)


def _validate_rule(raw: Any) -> dict:
    _require(isinstance(raw, dict), "each rule must be an object")
    rule_id = str(raw.get("id") or "").strip()
    _require(bool(rule_id), "rule id is required")
    tool = str(raw.get("tool") or "").strip()
    _require(bool(tool), f"rule {rule_id}: tool is required")
    arg = str(raw.get("arg") or "").strip()
    _require(bool(arg), f"rule {rule_id}: arg is required")
    op = str(raw.get("op") or "").strip()
    _require(op in VALID_OPS, f"rule {rule_id}: op must be one of {sorted(VALID_OPS)}")
    action = str(raw.get("action") or "deny").strip() or "deny"
    _require(action in VALID_ACTIONS, f"rule {rule_id}: action must be 'deny' or 'ask'")
    note = str(raw.get("note") or "")
    value = raw.get("value")

    if op in ("one_of", "domain_in"):
        _require(
            isinstance(value, list) and bool(value)
            and all(isinstance(v, str) and v.strip() for v in value),
            f"rule {rule_id}: {op} requires a non-empty list of non-empty strings for value",
        )
    elif op in ("prefix", "not_prefix"):
        _require(
            isinstance(value, str) and bool(value),
            f"rule {rule_id}: {op} requires a non-empty string value",
        )
    elif op == "regex":
        _require(
            isinstance(value, str) and bool(value),
            f"rule {rule_id}: regex requires a non-empty string value",
        )
        _require(
            len(value) <= MAX_REGEX_PATTERN_LEN,
            f"rule {rule_id}: regex pattern too long (max {MAX_REGEX_PATTERN_LEN} chars)",
        )
        try:
            re.compile(value)
        except re.error as exc:
            raise RuleError(f"rule {rule_id}: invalid regex: {exc}") from exc
    elif op == "max_len":
        _require(
            isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0,
            f"rule {rule_id}: max_len requires a non-negative number for value",
        )
    elif op == "equals":
        _require(value is not None, f"rule {rule_id}: equals requires a value")

    return {
        "id": rule_id, "tool": tool, "arg": arg, "op": op, "value": value,
        "action": action, "note": note,
    }


def validate_rules(rules: Any) -> list:
    """Validate a whole `tool_arg_rules` list, raising `RuleError` (message
    suitable for a 400 response) on the first problem. Returns the
    normalized list on success."""
    _require(isinstance(rules, list), "tool_arg_rules must be a list")
    seen: set = set()
    checked = []
    for raw in rules:
        rule = _validate_rule(raw)
        _require(rule["id"] not in seen, f"duplicate rule id {rule['id']!r}")
        seen.add(rule["id"])
        checked.append(rule)
    return checked


def evaluate(tool_name: str, args: Any) -> Optional[Decision]:
    """The first violated rule for this call, or None.

    `args` is the tool's arguments as a dict (already parsed from whatever
    wire form the caller used — JSON body, fenced-block content, native
    function-call arguments). A non-dict `args` is treated as `{}`: every
    arg path in it resolves to missing.
    """
    tool_name = str(tool_name or "")
    if not isinstance(args, dict):
        args = {}
    if not tool_name:
        return None

    from src.settings import get_setting

    try:
        raw_rules = get_setting("tool_arg_rules", []) or []
    except Exception:  # noqa: BLE001 - settings unavailable is not a policy hit
        return None
    if not isinstance(raw_rules, list) or not raw_rules:
        return None

    for raw in raw_rules:
        if not isinstance(raw, dict):
            continue
        tool_pattern = str(raw.get("tool") or "")
        if not tool_pattern or not fnmatch.fnmatchcase(tool_name, tool_pattern):
            continue
        op = str(raw.get("op") or "")
        arg_path = str(raw.get("arg") or "")
        if op not in VALID_OPS or not arg_path:
            continue
        rule_value = raw.get("value")
        actual = _get_path(args, arg_path)
        if not _violates(op, actual, rule_value):
            continue
        action = str(raw.get("action") or "deny")
        if action not in VALID_ACTIONS:
            action = "deny"
        return Decision(
            rule_id=str(raw.get("id") or ""),
            tool=tool_name,
            arg=arg_path,
            op=op,
            value=rule_value,
            action=action,
            note=str(raw.get("note") or ""),
        )
    return None


def override_security_decision(security_decision: Any, arg_policy_decision: Optional[Decision]) -> Any:
    """The one bit of glue between an "ask" argument rule and the EXISTING
    human approval flow (src/agent_loop.py's `security_decision` /
    `ToolRunSecurityContext.decision_for`, which the approval-card branch a
    few lines below it already keys off): if `arg_policy_decision` fired with
    `action == "ask"` and `security_decision` is currently allowed, return a
    new not-allowed decision carrying the rule's own message as the reason
    the approval card shows. Otherwise `security_decision` is returned
    unchanged — a rule that fires "deny" is a separate, earlier branch that
    never reaches this at all, and an already-not-allowed decision (some
    other gate already refused the call) is never overwritten, so its own
    reason keeps showing.
    """
    if (
        arg_policy_decision is not None
        and arg_policy_decision.action == "ask"
        and getattr(security_decision, "allowed", False)
    ):
        from src.tool_capabilities import ToolGateDecision

        return ToolGateDecision(allowed=False, reason=arg_policy_decision.message())
    return security_decision


def extract_tool_args(tool_name: str, content: Any) -> dict:
    """Best-effort arguments dict for a tool call whose wire form is a fenced
    block's raw text `content` rather than an already-structured dict (the
    shape `evaluate` needs). JSON object content decodes directly; the
    legacy line-parsed builtins (bash, web_search, ...) reuse
    `src.tool_execution`'s own per-tool parsers so a rule on `arg: "command"`
    for `bash` sees the same value execution does. Anything else falls back
    to `{"content": <raw text>}` so a rule can still be written against the
    whole payload."""
    if isinstance(content, dict):
        return content
    raw = "" if content is None else str(content)
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    try:
        from src.tool_execution import _MCP_ARG_PARSERS
        parser = _MCP_ARG_PARSERS.get(str(tool_name or ""))
        if parser:
            parsed = parser(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:  # noqa: BLE001 - fall through to the generic wrapper
        pass
    return {"content": raw}
