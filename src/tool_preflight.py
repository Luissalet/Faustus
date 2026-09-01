"""Tool preflight (FAUSTUS): don't offer tools that cannot work this turn.

Observed live, with a linked folder, Agent mode, and a chat that is NOT inside
a project: the model called `project_context` twice in a row and both calls
came back `exit_code=1` — "This chat is not attached to a project". The tool
could not have worked at any point in that turn. Two rounds at 1.3 tok/s, plus
two failures in the transcript, spent on a fact the runtime already knew before
the first token. The same log carried `SMTP not configured — add an Email
Account in Settings` and `IMAP not configured`, so the email tools were being
offered on a box with no mailbox either. It is a pattern, not one tool.

A large model reads a tool description, works out that it does not apply, and
moves on. A 9B model tries it, fails, and sometimes tries again. Every
impossible tool in the list is a trap, and each one also spends schema tokens
out of a window that is already tight. So the runtime removes what it can prove
cannot succeed, instead of asking the model to be smarter — which is the whole
point of this fork.

Discipline, mirroring `WORKSPACE_TOOL_FLOOR` in `src/agent_loop.py`:

  * A rule states an impossibility that is STRUCTURAL and CHECKABLE NOW, never
    a hunch about what the turn is "probably" about. Where a rule can reuse the
    exact check the tool itself performs, it calls that same function, so the
    prediction and the runtime failure cannot drift apart.
  * This only ever REMOVES tools. It has no path that adds one.
  * A wrong removal is far worse than a missed one: it takes away something
    that worked. Every rule below therefore fails OPEN — any doubt, any raised
    exception, any half-known state, and the tool stays on the list.
  * The workspace floor outranks every rule here. `prune_for_turn` subtracts
    it before returning, and `src/agent_loop.py` passes it in, so a rule can
    never blind a workspace agent — floor and rule disagree, floor wins.

Rules deliberately NOT written (each one investigated and rejected):

  * `web_search` / `web_fetch` with "no search provider configured" — there is
    always a provider. `search_provider` defaults to `searxng`,
    `SEARXNG_INSTANCE` defaults to `http://localhost:8080`, and
    `search_fallback_chain` defaults to `["duckduckgo"]`, which needs no API
    key; `services/search/core.py:_build_provider_chain` appends that fallback
    whenever the user chain is empty. There is no reachable "no provider"
    state, so there is no rule.
  * `edit_image` when image generation is off — `image_gen_enabled` gates
    `generate_image`, not `edit_image`, which posts to the gallery's
    upscale/rembg/inpaint routes and works without any image model.
  * File/shell tools with no bound workspace — they still operate on the
    default working directory. Not an impossibility.
  * Cookbook, research and session tools — no cheap, certain "this cannot
    work" fact; the failure modes are runtime, not structural.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)


# ── Context ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PreflightContext:
    """The turn facts a rule is allowed to look at.

    `tools` is the selection the model is about to be shown. It is an
    optimisation and a scope limit in one: a rule whose tools are not on the
    table is never run, so a turn with no email tools never touches the email
    tables. `None` means "the caller has not narrowed the list", and every
    rule runs.
    """

    session_id: str = ""
    # Kept as Optional[str], never normalised to "": `services.projects._owned`
    # treats owner=None as "visible to everyone" and owner="" as a real user id
    # that matches nothing. A rule that replicates a tool's own check has to
    # hand it the same value the tool would get from the loop.
    owner: Optional[str] = None
    tools: Optional[FrozenSet[str]] = None


def _coerce_context(ctx) -> PreflightContext:
    """Accept a PreflightContext, a mapping, or anything with the attributes."""
    if isinstance(ctx, PreflightContext):
        return ctx
    if isinstance(ctx, Mapping):
        get = ctx.get
    else:
        def get(key, default=None):
            return getattr(ctx, key, default)
    tools = get("tools", None)
    owner = get("owner", None)
    return PreflightContext(
        session_id=str(get("session_id", "") or ""),
        owner=owner if owner is None else str(owner),
        tools=None if tools is None else frozenset(str(t) for t in tools),
    )


# ── Rule 1: project tools in a chat that is not inside a project ──────────

# `project_context` and `search_project_chats` both open with the same two
# lines in `src/tool_execution.py`:
#
#     project = project_for_session(session_id or "", owner)
#     if not project:
#         result = {"error": "This chat is not attached to a project", ...}
#
# so the rule calls that same resolver rather than re-deriving "is this chat in
# a project?" from session rows. `project_for_session` never raises (it
# swallows every failure and returns None on the hot chat path), which means
# the prediction is the runtime answer, not an approximation of it: whenever
# this rule fires, the tool WOULD have returned that error.
#
# It is also the same call the turn route already makes in the other
# direction — `routes/chat_routes.py` force-includes these two tools when
# `project_for_session` finds a project. Add-when-there-is-one and
# remove-when-there-is-not now agree by construction, because both ask the one
# function; they cannot drift into forcing a tool the preflight then removes.
PROJECT_TOOLS: FrozenSet[str] = frozenset({"project_context", "search_project_chats"})

PROJECT_REASON = "this chat is not attached to a project"


def _project_rule(ctx: PreflightContext) -> Dict[str, str]:
    from services.projects import project_for_session

    if project_for_session(ctx.session_id or "", ctx.owner):
        return {}
    return {name: PROJECT_REASON for name in PROJECT_TOOLS}


# ── Rule 2: email tools with no mailbox anywhere ──────────────────────────

EMAIL_REASON = "no email account is configured — add an Email Account in Settings"


def _email_tool_names() -> FrozenSet[str]:
    """Every spelling of every built-in email tool.

    `email_tool_policy_names` is the same alias expansion the loop's own
    denylist checks use, so a bare `send_email` and the qualified
    `mcp__email__send_email` are pruned together — a denylist written in one
    spelling and a call made in the other is a known way for a gate to be
    walked straight past.
    """
    from src.tool_security import BUILTIN_EMAIL_TOOLS, email_tool_policy_names

    names: set = set()
    for tool in BUILTIN_EMAIL_TOOLS:
        names |= set(email_tool_policy_names(tool))
    return frozenset(names)


def _email_rule(ctx: PreflightContext) -> Dict[str, str]:
    # `_get_email_config` is email_helpers' own account resolver, and it is the
    # code that logged "SMTP not configured — add an Email Account in Settings"
    # and "IMAP not configured" in the observed run. It reads the same
    # `email_accounts` table the built-in email MCP server reads, so "no row
    # here" is "no row there". It is called with NO owner on purpose, and the
    # question asked of it is deliberately the weakest one that is still
    # decisive: does ANY mailbox exist on this box at all?
    #
    # Its docstring warns that the unscoped fallback is not owner-filtered.
    # That warning is about SERVING another user's credentials; nothing is
    # served here. The returned dict is read for two booleans — "did a row
    # resolve" and "are the legacy keys filled in" — and then dropped. Scoping
    # the lookup to the owner would make the rule WRONG in the other direction:
    # on a single-user box with a legacy ownerless account row and an owner id
    # that is not an email address, the owner-scoped query resolves nothing
    # while the account works perfectly, and the rule would delete a whole
    # working toolset. Fail open, always.
    from routes.email_helpers import _get_email_config

    cfg = _get_email_config() or {}
    if cfg.get("account_id"):
        return {}  # at least one enabled account row exists

    # No row at all: the legacy flat keys in data/settings.json and the
    # SMTP_*/IMAP_* env vars are the only remaining way email can work. These
    # are the two conditions `_get_email_config` itself warns on, in the branch
    # that produced the observed log lines (routes/email_helpers.py, "Legacy
    # fallback"); `_smtp_ready` is email_routes' own send-side predicate and is
    # used instead of re-spelling the SMTP half, because it also accepts an
    # OAuth account that has no stored password.
    from routes.email_routes import _smtp_ready

    if _smtp_ready(cfg):
        return {}
    if cfg.get("imap_host") and cfg.get("imap_user") and cfg.get("imap_password"):
        return {}
    return {name: EMAIL_REASON for name in _email_tool_names()}


# ── Rule 3: api_call with no integrations registered ──────────────────────

INTEGRATION_REASON = "no service integrations are configured"


def _integration_rule(ctx: PreflightContext) -> Dict[str, str]:
    # `do_api_call` (src/tools/system.py) resolves its `integration` argument
    # against `load_integrations()` and returns
    # "No integration matching '<x>'. Available: none configured" when the list
    # is empty. With zero integrations there is no argument that can match, so
    # every possible call fails. Only the empty case is treated as impossible:
    # a registered-but-disabled integration still matches by name there, so
    # "all disabled" is NOT claimed to be a dead end.
    from src.integrations import load_integrations

    if load_integrations():
        return {}
    return {"api_call": INTEGRATION_REASON}


# ── Registry ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PreflightRule:
    name: str
    # Resolved at call time, not frozen at import: the email set is derived
    # from `src.tool_security`, and the two must not be able to drift.
    covers: Callable[[], FrozenSet[str]]
    check: Callable[[PreflightContext], Dict[str, str]]


RULES = (
    PreflightRule("project", lambda: PROJECT_TOOLS, _project_rule),
    PreflightRule("email", _email_tool_names, _email_rule),
    PreflightRule("integrations", lambda: frozenset({"api_call"}), _integration_rule),
)


def unusable_tools(ctx) -> Dict[str, str]:
    """Tools that cannot succeed in this turn, mapped to a readable reason.

    The reason is a sentence fragment written for two readers: the
    `[agent-debug]` log line, and the model itself when it calls a pruned tool
    anyway ("this chat is not attached to a project" closes the loop in one
    round; "unknown tool" opens it).

    Never raises. A rule that blows up is logged and skipped, and the turn goes
    out with that rule's tools intact — degrading to the old behaviour, which
    is merely wasteful, instead of to a turn with tools missing for no reason.
    """
    context = _coerce_context(ctx)
    pruned: Dict[str, str] = {}
    for rule in RULES:
        try:
            covers = rule.covers()
            if context.tools is not None and not (covers & context.tools):
                continue  # none of this rule's tools are on the table
            found = rule.check(context) or {}
            for tool, reason in found.items():
                pruned.setdefault(str(tool), str(reason))
        except Exception as exc:  # noqa: BLE001 - a broken rule must not cost tools
            logger.debug("[tool-preflight] rule %r skipped: %s", rule.name, exc)
    if context.tools is not None:
        pruned = {t: r for t, r in pruned.items() if t in context.tools}
    return pruned


def prune_for_turn(
    ctx,
    protected: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """`unusable_tools` with a set that can never be removed subtracted out.

    `protected` is the workspace tool floor. The floor is an invariant the loop
    states about itself; a preflight rule is a prediction. When the two
    disagree the floor wins, unconditionally and without argument — a rule that
    ever reaches for a floor tool is a bug, and it gets logged as one.
    """
    pruned = unusable_tools(ctx)
    keep = {str(name) for name in (protected or ())}
    if keep:
        collided = sorted(set(pruned) & keep)
        if collided:
            logger.warning(
                "[tool-preflight] rule tried to remove floor tools %s — floor wins",
                collided,
            )
            pruned = {t: r for t, r in pruned.items() if t not in keep}
    return pruned
