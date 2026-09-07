"""Which routes an API token may reach, declared in one place (AUTH-1 / B-011).

The middleware validated an `ody_` bearer token, stamped its scopes on the
request and let it through. Whether the scope was *checked* was then up to each
endpoint, and four of them did it. So a token minted for chat could reach the
skills routes, create resources under the technical owner `api`, start test
runs and audits, and spend the model budget — not an admin bypass, but an
authorization matrix with holes in the shape of whoever remembered.

This module is the matrix, and it is **deny by default**: a bearer token
reaches a route only if a rule here says which scope opens it. A route added
tomorrow is closed to tokens until someone writes it down, which is the right
direction for the mistake to point.

Cookie sessions and the in-process internal token are NOT governed here — they
have their own gates (`require_admin`, `require_human`, per-route owner
checks). This is about the identity that lives outside the app.

Stdlib only, no imports from `src`: the auth middleware runs before everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Optional, Tuple

#: Every scope a token can carry (routes/api_token_routes.py owns the catalogue;
#: this list exists so a typo in a rule below is caught by a test, not by a 403
#: in production).
KNOWN_SCOPES: FrozenSet[str] = frozenset({
    "chat",
    "todos:read", "todos:write",
    "documents:read", "documents:write",
    "email:read", "email:draft", "email:send",
    "calendar:read", "calendar:write",
    "memory:read", "memory:write",
    "cookbook:read", "cookbook:launch",
    "agents:dispatch",
})

READ_METHODS: FrozenSet[str] = frozenset({"GET", "HEAD", "OPTIONS"})
WRITE_METHODS: FrozenSet[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ANY_METHOD: FrozenSet[str] = READ_METHODS | WRITE_METHODS


@dataclass(frozen=True)
class Rule:
    """One line of the matrix.

    `path` is either an exact application path or, with `prefix=True`, the
    start of one. `scopes` is any-of: a token holding one of them passes.
    `effect` is what the audit calls the effect class, and it is written down
    even where nothing reads it yet — the point of the matrix is that the
    answer exists before someone needs it.
    """

    methods: FrozenSet[str]
    path: str
    scopes: Tuple[str, ...]
    prefix: bool = False
    effect: str = "read"          # read | reversible | external | admin
    owner_rule: str = "own"       # own | any | none
    note: str = ""
    requires: Tuple[str, ...] = ()  # all-of, in addition to the any-of scopes

    def matches(self, method: str, path: str) -> bool:
        if method.upper() not in self.methods:
            return False
        if self.prefix:
            # Segment-aware on purpose: `/api/dispatcher-x` must not inherit
            # the rule written for `/api/dispatch`.
            base = self.path.rstrip("/")
            return path == base or path.startswith(base + "/")
        if "{" in self.path:
            expected, actual = self.path.split("/"), path.split("/")
            return len(expected) == len(actual) and all(
                bool(value) if part.startswith("{") and part.endswith("}") else part == value
                for part, value in zip(expected, actual))
        return path == self.path


def _read(path: str, *scopes: str, prefix: bool = False, note: str = "") -> Rule:
    return Rule(READ_METHODS, path, scopes, prefix=prefix, effect="read", note=note)


def _write(path: str, *scopes: str, prefix: bool = False, effect: str = "reversible",
           note: str = "") -> Rule:
    return Rule(WRITE_METHODS, path, scopes, prefix=prefix, effect=effect, note=note)


def _any(path: str, *scopes: str, prefix: bool = False, effect: str = "reversible",
         note: str = "") -> Rule:
    return Rule(ANY_METHOD, path, scopes, prefix=prefix, effect=effect, note=note)


#: The matrix. Order matters only for readability: a path matches at most one
#: family here, and `api_rule_for` returns the first rule that matches.
#:
#: What is deliberately NOT here is the point of the file. Skills, projects,
#: settings, models management, research, workspaces, backups, the agent's own
#: tool routes — a bearer token has no business in any of them, and until this
#: change it could reach every one.
API_TOKEN_RULES: Tuple[Rule, ...] = (
    # The chat surface an integration is minted for.
    Rule(WRITE_METHODS, "/api/v1/chat", ("chat",), effect="external",
         note="one prompt, one answer; the token's owner pays for it"),
    _read("/api/models", "chat",
          note="which models the token's owner may name in a chat call"),

    # Codex/Claude integrations: explicit methods/paths, matching the route's
    # owner checks. A new endpoint does not inherit blanket token admission.
    _read("/api/codex/capabilities", *sorted(KNOWN_SCOPES), note="the token's available capabilities"),
    _read("/api/codex/plugin.zip", *sorted(KNOWN_SCOPES), note="download the integration bundle"),
    _read("/api/codex/todos", "todos:read", "todos:write", note="owned notes and todos"),
    Rule(frozenset({"POST"}), "/api/codex/todos", ("todos:read", "todos:write"),
         effect="reversible", note="action route additionally requires write scope for mutations"),
    *(_read(path, "email:read", "email:draft", "email:send", note="owned email reads")
      for path in ("/api/codex/emails", "/api/codex/emails/{uid}")),
    Rule(frozenset({"POST"}), "/api/codex/emails/draft-document", ("email:draft", "email:send"),
         effect="reversible", requires=("documents:write",), note="draft email as an owned document"),
    Rule(frozenset({"POST"}), "/api/codex/emails/draft", ("email:draft", "email:send"),
         effect="reversible", note="stage an owned email draft"),
    Rule(frozenset({"POST"}), "/api/codex/emails/send", ("email:send",),
         effect="external", note="send email through the owned account"),
    _read("/api/codex/memory", "memory:read", "memory:write", note="owned memories"),
    Rule(frozenset({"POST"}), "/api/codex/memory", ("memory:write",), effect="reversible", note="save owned memory"),
    Rule(frozenset({"DELETE"}), "/api/codex/memory/{memory_id}", ("memory:write",), effect="reversible", note="delete owned memory"),
    _read("/api/codex/calendar/events", "calendar:read", "calendar:write", note="owned calendar events"),
    Rule(frozenset({"POST"}), "/api/codex/calendar/events", ("calendar:write",), effect="reversible", note="save owned event"),
    Rule(frozenset({"DELETE"}), "/api/codex/calendar/events/{uid}", ("calendar:write",), effect="reversible", note="delete owned event"),
    *(_read(path, "documents:read", "documents:write", note="owned documents")
      for path in ("/api/codex/documents", "/api/codex/documents/{doc_id}")),
    Rule(frozenset({"POST"}), "/api/codex/documents", ("documents:write",), effect="reversible", note="create owned document"),
    Rule(frozenset({"DELETE"}), "/api/codex/documents/{doc_id}", ("documents:write",), effect="reversible", note="delete owned document"),
    *(_read("/api/codex/cookbook/" + path, "cookbook:read", "cookbook:launch", note="inspect Cookbook; route also checks admin")
      for path in ("tasks", "servers", "output/{session_id}", "cached", "presets")),
    *(Rule(frozenset({"POST"}), "/api/codex/cookbook/" + path, ("cookbook:launch",),
           effect="external", note="control Cookbook processes; route also checks admin")
      for path in ("serve", "stop/{session_id}", "preset/{name}", "adopt")),

    # Dispatching local workers from outside the app (Fable, Claude Desktop, a
    # script). `routes/dispatch_routes.py` additionally requires the token's
    # owner to be an admin, because this starts processes on the machine.
    Rule(ANY_METHOD, "/api/dispatch", ("agents:dispatch",), prefix=True,
         effect="external", note="starts real work on this machine"),
    _read("/api/changesets/from-dispatch/", "agents:dispatch", prefix=True,
          note="the diff a dispatched job produced, for the coordinator that asked for it"),
)


def api_rule_for(method: str, path: str) -> Optional[Rule]:
    """The rule that opens `path` to an API token, or None when none does."""
    for rule in API_TOKEN_RULES:
        if rule.matches(method, path):
            return rule
    return None


def api_token_allowed(method: str, path: str, scopes: Iterable[str]) -> Tuple[bool, str]:
    """(allowed, reason). The reason is what the 403 says, so it has to be true.

    Deny by default: no rule means the route was never opened to tokens, and
    saying so is more useful than "forbidden".
    """
    rule = api_rule_for(method, path)
    if rule is None:
        return False, ("this route is not part of the API-token surface: tokens reach only "
                       "the chat, codex-skill and dispatch routes")
    held = {str(s).strip() for s in (scopes or ()) if str(s).strip()}
    missing = set(rule.requires) - held
    if missing:
        return False, "API token missing required scope: " + " and ".join(sorted(missing))
    if held.intersection(rule.scopes):
        return True, ""
    required = " or ".join(sorted(rule.scopes)) if len(rule.scopes) <= 3 else "one of its scopes"
    return False, f"API token missing required scope: {required}"


def api_surface() -> Dict[str, Tuple[str, ...]]:
    """Legacy any-of surface view; inspect Rule.requires for additional all-of gates."""
    out: Dict[str, Tuple[str, ...]] = {}
    for rule in API_TOKEN_RULES:
        key = f"{'|'.join(sorted(rule.methods))} {rule.path}{'*' if rule.prefix else ''}"
        out[key] = rule.scopes
    return out
