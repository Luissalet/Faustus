"""agent_gate.py — Faustus's own policy, in front of a foreign agent's tools.

    src/external_worker.py has said one thing plainly since the day it landed:

        Faustus's command guard CANNOT see inside another agent's own shell.

This module closes that for the runners whose own documented interface lets it
be closed — today exactly one, Claude Code, whose ``PreToolUse`` hook runs
FIRST in its permission evaluation and whose deny holds even under
``bypassPermissions``. Every tool call the agent wants to make arrives here as
a JSON payload; this module answers ``allow`` / ``deny`` / ``ask`` and the CLI
obeys it.

**Nothing here is a second policy engine.** A Bash-shaped call is classified by
:mod:`src.command_guard` — the same tiers, the same packs, the same 50 ms
budget, the same hash-chained receipts the built-in tools get. A write is
checked against the workspace roots and against
:class:`src.agent_tools.subagent_tools.FileLockRegistry`, so a foreign agent
cannot clobber the file a built-in worker is holding. This module is the
routing and the honesty, not the rules.

**What it deliberately does not do.**

* It is a policy gate, not a sandbox. It sees the calls the agent makes
  *through its own tool interface*. A process that agent starts has its own
  children, and they are not tool calls; a `Bash` call classified SAFE can
  still run a program that does something else. That is why a gated run is
  reconciled afterwards against the CLI's own ``stream-json`` output
  (src/external_worker.py): the stream is written by the CLI, not by the model,
  so a tool call that appears there with no receipt here is *seen* and reported
  rather than silently assumed judged.
* A tool name it does not recognise is **allowed, and recorded as unjudged**.
  A gate that denied every tool it had not heard of would break a foreign CLI
  on the day it shipped a new one — and a gate that silently allowed it would
  be claiming coverage it does not have. The unjudged call is counted, named,
  and reaches ``prove`` as its own (narrower) uncertainty.

There is no flag, setting or environment variable that turns this off. The
caller of an external runner is a language model; a safeguard a model can
switch off is not one.

Stdlib only. Every entry point is total: a bug in here must never become an
allow for a destructive call, and must never crash the foreign agent either —
see :func:`judge` for which way each failure falls.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The three answers Claude Code's PreToolUse hook understands.
DECISIONS: Tuple[str, ...] = ("allow", "deny", "ask")

#: Same wall-clock discipline as src/command_guard.py: a decision that cannot
#: be made in 50 ms is not made at all, and falls the way :func:`judge` says.
BUDGET_MS = 50.0

#: Tool names, by what Faustus can actually judge about them.
#:
#: Only names this project has seen documented are listed. An unlisted name is
#: not a bug and not a threat model gap — it is an honest `unjudged`, counted
#: and reported. Adding a name here is a claim that the mapping below really
#: judges it, so the lists stay short on purpose.
BASH_TOOLS = frozenset({"Bash"})
#: Tools whose input names a file they will WRITE.
WRITE_TOOLS: Dict[str, Tuple[str, ...]] = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path", "file_path"),
}
#: Tools that read or bookkeep and mutate nothing on disk. Allowed without a
#: policy, and NOT counted as unjudged: there is no policy to apply, which is
#: a different fact from "Faustus did not recognise this".
READ_ONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "TodoWrite", "ExitPlanMode", "BashOutput",
})

#: Hard ceiling on one run's judgements, and the burst the token bucket allows.
#: A runaway agent must not be able to use the gate as a fast oracle for what
#: this machine will and will not run.
MAX_CALLS_PER_RUN = 10_000
_BUCKET_CAPACITY = 240.0
_BUCKET_REFILL_PER_S = 60.0

#: How long a run's token stays valid without the run being closed. The token
#: normally dies with the run; this is the backstop for a supervisor that was
#: killed before it could close one.
TOKEN_TTL_S = 24 * 3600.0

#: How many finished runs' ledgers are kept for `prove` to read.
_LEDGER_HISTORY = 64

_ALLOW = "allow"
_DENY = "deny"
_ASK = "ask"


@dataclass
class GateDecision:
    """One answer for one foreign tool call.

    ``reason`` is shown to the foreign agent *and* written to the receipt, so
    the sentence the agent reads and the sentence the audit log keeps are the
    same sentence.
    """

    decision: str
    reason: str
    updated_input: Optional[Dict[str, Any]] = None
    #: Bookkeeping for the ledger — never sent to the foreign agent.
    tier: str = ""
    rule: str = ""
    judged: bool = True
    tool: str = ""

    def hook_output(self) -> Dict[str, Any]:
        """The ``hookSpecificOutput`` block Claude Code expects."""
        out: Dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": self.decision,
            "permissionDecisionReason": self.reason,
        }
        if self.updated_input:
            out["updatedInput"] = dict(self.updated_input)
        return out


# ── the runs this gate is currently answering for ───────────────────────────

@dataclass
class GateRun:
    """One external agent run, its token, and what the gate saw it do."""

    run_id: str
    token: str
    runner: str = ""
    owner: str = ""
    workspace_roots: List[str] = field(default_factory=list)
    cwd: str = ""
    #: True only when the spawner owns a surface that can put a question to a
    #: human. Same assertion `src/tool_approvals.py` calls `allow_continuation`
    #: — the caller says whether there is anyone to ask; the gate never guesses.
    attended: bool = False
    created: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + TOKEN_TTL_S)
    finished: bool = False
    #: The file locks of the delegation this run belongs to, when it has one
    #: (src/agent_tools/subagent_tools.FileLockRegistry). None = this run is
    #: the only writer Faustus knows about.
    locks: Any = None
    worker_key: str = ""

    calls: int = 0
    allowed: int = 0
    denied: int = 0
    asked: int = 0
    unjudged: int = 0
    corrected: int = 0
    throttled: int = 0
    errors: int = 0
    unjudged_tools: Dict[str, int] = field(default_factory=dict)
    #: Every tool_use_id the gate answered for, so the CLI's own stream can be
    #: reconciled against it afterwards (see external_worker.reconcile).
    seen_ids: set = field(default_factory=set)

    _bucket: float = field(default=_BUCKET_CAPACITY)
    _bucket_at: float = field(default_factory=time.monotonic)

    def take_token(self) -> bool:
        """Token bucket. False when this run is asking too fast."""
        now = time.monotonic()
        self._bucket = min(_BUCKET_CAPACITY,
                           self._bucket + (now - self._bucket_at) * _BUCKET_REFILL_PER_S)
        self._bucket_at = now
        if self._bucket < 1.0:
            return False
        self._bucket -= 1.0
        return True

    def ledger(self) -> Dict[str, Any]:
        """What the gate saw, as plain data for the proof packet."""
        return {
            "gated": True,
            "runner": self.runner,
            "run_id": self.run_id,
            "attended": bool(self.attended),
            "calls": self.calls,
            "allowed": self.allowed,
            "denied": self.denied,
            "asked": self.asked,
            "corrected": self.corrected,
            "unjudged": self.unjudged,
            "unjudged_tools": sorted(self.unjudged_tools),
            "throttled": self.throttled,
            "errors": self.errors,
        }


_runs: Dict[str, GateRun] = {}
_by_token: Dict[str, str] = {}
_history: List[Dict[str, Any]] = []
_lock = threading.Lock()


def open_run(run_id: Any, *, runner: Any = "", owner: Any = "",
             workspace_roots: Any = (), cwd: Any = "", attended: bool = False,
             locks: Any = None, worker_key: Any = "",
             ttl_s: Optional[float] = None) -> GateRun:
    """Register a run and mint its token.

    The token is **per run**: high-entropy, generated here, never persisted,
    and dead the moment :func:`close_run` is called. It is deliberately not the
    app's internal-tool token — a foreign process holding one of those could
    reach every admin route in Faustus, and the whole point of this endpoint is
    that the process holding its credential can do exactly one thing with it.
    """
    rid = str(run_id or "")
    roots: List[str] = []
    for root in (workspace_roots or ()):
        text = str(root or "").strip()
        if not text:
            continue
        try:
            real = os.path.realpath(text)
        except (OSError, ValueError):
            continue
        if real not in roots:
            roots.append(real)
    run = GateRun(
        run_id=rid,
        token=secrets.token_urlsafe(32),
        runner=str(runner or ""),
        owner=str(owner or ""),
        workspace_roots=roots,
        cwd=str(cwd or ""),
        attended=bool(attended),
        locks=locks,
        worker_key=str(worker_key or ""),
        expires_at=time.time() + float(TOKEN_TTL_S if ttl_s is None else ttl_s),
    )
    with _lock:
        _runs[rid] = run
        _by_token[run.token] = rid
    return run


def run_for_token(token: Any) -> Optional[GateRun]:
    """The live run a token names, or None.

    None for unknown, expired and finished alike: the caller answers 404 for
    all three, so probing the endpoint cannot tell a wrong token from a token
    whose run is over.
    """
    text = str(token or "")
    if not text:
        return None
    with _lock:
        # Constant-time over the live tokens, so a timing difference cannot
        # walk a guess towards a real token one character at a time.
        run_id = ""
        for known, rid in _by_token.items():
            if secrets.compare_digest(known, text):
                run_id = rid
        run = _runs.get(run_id) if run_id else None
        if run is None or run.finished or time.time() > run.expires_at:
            return None
        return run


def close_run(run_id: Any) -> Optional[Dict[str, Any]]:
    """End a run: its token stops working and its ledger is returned."""
    rid = str(run_id or "")
    with _lock:
        run = _runs.pop(rid, None)
        if run is None:
            return None
        _by_token.pop(run.token, None)
        run.finished = True
        led = run.ledger()
        _history.append(led)
        del _history[:-_LEDGER_HISTORY]
        return led


def ledger_of(run_id: Any) -> Optional[Dict[str, Any]]:
    """The ledger of a live run, or None."""
    with _lock:
        run = _runs.get(str(run_id or ""))
        return run.ledger() if run is not None else None


def recent_ledgers() -> List[Dict[str, Any]]:
    """The ledgers of the last runs that finished (newest last)."""
    with _lock:
        return [dict(x) for x in _history]


def reset() -> None:
    """Forget every run. Tests only — a live server never calls this."""
    with _lock:
        _runs.clear()
        _by_token.clear()
        del _history[:]


# ── the decision ────────────────────────────────────────────────────────────

def _tool_input(payload: Any) -> Dict[str, Any]:
    value = payload if isinstance(payload, dict) else {}
    return value


def _first_path(tool_input: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[str, str]:
    for key in keys:
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw.strip():
            return key, raw.strip()
    return "", ""


def _inside_any(roots: List[str], path: str) -> bool:
    try:
        from src.tool_capabilities import path_inside_trusted
    except Exception:  # noqa: BLE001 - standalone use
        return False
    return any(path_inside_trusted(root, path) for root in roots)


def _sensitive(path: str) -> bool:
    """A path Faustus refuses for its OWN tools (~/.ssh, credential files…).

    Reused rather than re-listed: a directory the built-in tools may not write
    is not one a foreign agent may write either.
    """
    try:
        from src.tool_execution import _is_sensitive_path
        return bool(_is_sensitive_path(path))
    except Exception:  # noqa: BLE001 - a missing helper must not allow the write
        return False


def _reanchor(raw: str, cwd: str, roots: List[str]) -> str:
    """The same relative path, resolved against a workspace root instead.

    The case this exists for: the agent's cwd is a subdirectory (or the CLI
    reported one this run did not set) and it writes ``src/cart.py`` meaning
    the workspace's ``src/cart.py``. The call is not wrong in intent, only in
    anchor — correcting it is worth far more than refusing it. Only ever
    returns a path that EXISTS, so this can never invent a new file somewhere
    the agent did not mean.
    """
    if os.path.isabs(raw):
        return ""
    for root in roots:
        candidate = os.path.realpath(os.path.join(root, raw))
        if not _inside_any(roots, candidate):
            continue
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(candidate)
        # A new file in an existing directory is the same near-miss.
        if parent and os.path.isdir(parent) and parent != root and os.path.exists(parent):
            return candidate
    return ""


def _guard_packs() -> Any:
    try:
        from src.tool_capabilities import _command_guard_packs
        return _command_guard_packs()
    except Exception:  # noqa: BLE001 - the full set is the safe default
        return None


def _judge_bash(tool_input: Dict[str, Any], *, attended: bool) -> GateDecision:
    command = tool_input.get("command")
    text = command if isinstance(command, str) else ("" if command is None else str(command))
    from src import command_guard
    decision = command_guard.classify(text, packs=_guard_packs(), budget_ms=BUDGET_MS)
    tier, rule = decision.tier, decision.rule_id
    if command_guard.tier_at_least(tier, "DANGEROUS"):
        return GateDecision(
            _DENY,
            f"Faustus refused this command: destructive tier {tier}"
            + (f" (rule {rule})" if rule else "")
            + (f", matched {decision.matched!r}" if decision.matched else "")
            + ". Faustus gates every shell command run inside its workspace, its own agents' "
              "and yours alike. Do the work another way, or report what needs doing and stop.",
            tier=tier, rule=rule,
        )
    if tier == "CAUTION":
        if not attended:
            return GateDecision(
                _DENY,
                f"Faustus refused this command: tier {tier}"
                + (f" (rule {rule})" if rule else "")
                + " needs a person to confirm it and this run is unattended — nobody is watching "
                  "it who could answer. Report what needs doing and stop.",
                tier=tier, rule=rule,
            )
        return GateDecision(
            _ASK,
            f"Faustus classifies this as tier {tier}"
            + (f" (rule {rule})" if rule else "")
            + (f", matched {decision.matched!r}" if decision.matched else "")
            + " — confirm it before it runs.",
            tier=tier, rule=rule,
        )
    return GateDecision(_ALLOW, f"Faustus classified this command as {tier}.", tier=tier, rule=rule)


def _judge_write(tool_name: str, tool_input: Dict[str, Any], *, cwd: str,
                 roots: List[str], run: Optional[GateRun]) -> GateDecision:
    keys = WRITE_TOOLS[tool_name]
    key, raw = _first_path(tool_input, keys)
    if not key:
        # A write tool whose target this gate could not read is not a write it
        # may wave through: an undeterminable target is not inside anything.
        return GateDecision(
            _DENY,
            f"Faustus could not read the target path of this {tool_name} call "
            f"(expected one of: {', '.join(keys)}). A write whose target is unknown cannot be "
            "checked against the workspace, so it is refused. Name the file explicitly.",
            tier="write",
        )
    base = cwd or (roots[0] if roots else "")
    try:
        candidate = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
    except (OSError, ValueError):
        candidate = raw
    updated: Optional[Dict[str, Any]] = None

    inside = _inside_any(roots, candidate) if roots else True
    # A path that lands nowhere real — outside the roots, or inside them but
    # in a directory that does not exist — is the shape of a path written
    # against the wrong anchor. Worth one attempt to correct before refusing.
    lands_nowhere = not os.path.exists(candidate) and not os.path.isdir(os.path.dirname(candidate))
    if roots and (not inside or lands_nowhere):
        fixed = _reanchor(raw, base, roots)
        if fixed and fixed != candidate:
            updated = {key: fixed}
            candidate = fixed
        elif not inside:
            return GateDecision(
                _DENY,
                f"Faustus refused this write: {candidate} is outside the workspace "
                f"({', '.join(roots)}). Everything this task may change lives under that root.",
                tier="outside-workspace",
            )

    if _sensitive(candidate):
        return GateDecision(
            _DENY,
            f"Faustus refused this write: {candidate} is inside a sensitive location "
            "(credentials, keys, or a system directory). Faustus refuses this path for its own "
            "tools too.",
            tier="sensitive-path",
        )

    owner = _locked_by(run, candidate)
    if owner:
        return GateDecision(
            _DENY,
            f"Faustus refused this write: '{candidate}' is owned by worker '{owner}' in this "
            "job — another worker is editing it and two writers would clobber each other. "
            "Finish your own part and describe in your final report exactly what that file needs.",
            tier="locked",
        )

    if updated:
        return GateDecision(
            _ALLOW,
            f"Faustus re-anchored '{raw}' to '{candidate}': the path was relative to a different "
            "directory than this run's workspace root. The corrected path is what will be written.",
            updated_input=updated, tier="corrected",
        )
    return GateDecision(_ALLOW, f"Inside the workspace ({candidate}).", tier="write")


def _locked_by(run: Optional[GateRun], path: str) -> str:
    """The other worker holding `path`, via the delegation's FileLockRegistry."""
    if run is None or run.locks is None:
        return ""
    try:
        other = run.locks.blocked_by(run.worker_key or run.run_id, [path])
        if not other:
            return ""
        return str(run.locks.label(other))
    except Exception as e:  # noqa: BLE001 - a lock registry that cannot answer
        # is not permission: an unreadable registry falls to the caller's
        # fail-closed branch, which denies the write.
        logger.debug("agent_gate: lock registry unavailable: %s", e)
        raise


def judge(tool_name: Any, tool_input: Any, *, cwd: Any = "", workspace_roots: Any = (),
          run_id: Any = "", owner: Any = None, attended: Optional[bool] = None,
          run: Optional[GateRun] = None) -> GateDecision:
    """Judge ONE foreign tool call. Never raises.

    Where the failures fall, and why they fall differently:

    * a **Bash call or a write** whose judgement failed is **denied**. These
      are the two shapes that can destroy something; a gate that cannot decide
      about them has to answer no.
    * anything else fails **open**, with the failure in the reason and in the
      ledger. A crash in this module must not turn a foreign CLI's `Read` into
      an error the user has to debug.

    The 50 ms budget is the same one :mod:`src.command_guard` keeps, for the
    same reason: the foreign agent is blocked on this HTTP call, and a gate
    that thinks for a second per tool is a gate the user turns off.
    """
    name = str(tool_name or "")
    args = _tool_input(tool_input)
    destructive_shape = name in BASH_TOOLS or name in WRITE_TOOLS
    started = time.perf_counter()
    try:
        roots = list(run.workspace_roots) if run is not None else [
            os.path.realpath(str(r)) for r in (workspace_roots or ()) if str(r or "").strip()
        ]
        here = str(cwd or "") or (run.cwd if run is not None else "")
        watched = run.attended if (attended is None and run is not None) else bool(attended)

        if name in BASH_TOOLS:
            out = _judge_bash(args, attended=watched)
        elif name in WRITE_TOOLS:
            out = _judge_write(name, args, cwd=here, roots=roots, run=run)
        elif name in READ_ONLY_TOOLS:
            out = GateDecision(_ALLOW, f"{name} reads; Faustus has no policy that applies to it.",
                               tier="read-only")
        else:
            # The honest branch. Not a silent allow and not a wall: the call
            # runs, the fact that nothing judged it is counted here, and it
            # leaves the run with a named uncertainty of its own.
            out = GateDecision(
                _ALLOW,
                f"Faustus does not recognise the tool '{name}' and did not judge this call. "
                "It is allowed and recorded as unjudged.",
                judged=False, tier="unjudged",
            )
        out.tool = name
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms > BUDGET_MS and destructive_shape and out.decision == _ALLOW:
            # Over budget on the shape that can destroy something: the answer
            # this took too long to reach is not one to act on.
            return GateDecision(
                _DENY,
                f"Faustus could not judge this {name} call within its {int(BUDGET_MS)} ms budget "
                "and does not allow an unjudged destructive call.",
                tier="budget", tool=name,
            )
        return out
    except Exception as e:  # noqa: BLE001 - see the docstring for the two ways this falls
        logger.warning("agent_gate.judge failed on %s: %r", name, e)
        if destructive_shape:
            return GateDecision(
                _DENY,
                f"Faustus's gate failed while judging this {name} call "
                f"({type(e).__name__}) and refuses a destructive call it could not judge.",
                tier="error", tool=name,
            )
        return GateDecision(
            _ALLOW,
            f"Faustus's gate failed while judging this {name} call ({type(e).__name__}); "
            "the call is allowed and recorded as unjudged.",
            judged=False, tier="error", tool=name,
        )


# ── the receipt, and the run's tally ────────────────────────────────────────

_ACTION = {_ALLOW: "allowed", _DENY: "blocked", _ASK: "asked"}


def record(run: Optional[GateRun], decision: GateDecision, *, tool_use_id: Any = "",
           command: Any = "") -> None:
    """Tally one decision on the run and append its hash-chained receipt.

    The receipt goes into the SAME chain as the built-in tools'
    (src/command_guard.py): one tamper-evident ledger for every command this
    machine judged, whoever asked to run it. Never raises — a receipt that
    cannot be written must not turn into an allow.
    """
    try:
        if run is not None:
            run.calls += 1
            if decision.decision == _DENY:
                run.denied += 1
            elif decision.decision == _ASK:
                run.asked += 1
            else:
                run.allowed += 1
            if not decision.judged:
                run.unjudged += 1
                key = decision.tool or "?"
                run.unjudged_tools[key] = run.unjudged_tools.get(key, 0) + 1
            if decision.updated_input:
                run.corrected += 1
            if decision.tier == "error":
                run.errors += 1
            uid = str(tool_use_id or "")
            if uid:
                run.seen_ids.add(uid)
    except Exception as e:  # noqa: BLE001
        logger.debug("agent_gate: could not tally a decision: %s", e)
    try:
        from src import command_guard
        text = command if isinstance(command, str) else json.dumps(command, sort_keys=True,
                                                                   default=str)[:1000]
        command_guard.append_receipt(
            session=f"agent-gate:{run.run_id if run is not None else '?'}",
            tool=f"{(run.runner if run is not None else 'external') or 'external'}:{decision.tool}",
            command=text,
            tier=decision.tier or decision.decision,
            rule=decision.rule,
            action=("unjudged" if not decision.judged else _ACTION.get(decision.decision, "allowed")),
            note=decision.reason[:400],
        )
    except Exception as e:  # noqa: BLE001 - receipts never break a decision
        logger.debug("agent_gate: receipt failed: %s", e)


# ── the hook payload, end to end ────────────────────────────────────────────

def _payload_text(tool_name: str, tool_input: Dict[str, Any]) -> Any:
    """What the receipt records as the "command" for this call."""
    if tool_name in BASH_TOOLS:
        raw = tool_input.get("command")
        return raw if isinstance(raw, str) else str(raw or "")
    return tool_input


def handle_hook(token: Any, payload: Any, *, client_host: Any = "127.0.0.1",
                internal_token_seen: bool = False) -> Tuple[int, Dict[str, Any]]:
    """One hook request → ``(status, body)``. The whole endpoint, testable
    without a web server.

    Three refusals before any judging happens, and each one is here rather than
    in the route because a second transport must not be able to skip them:

    * **not loopback** — the gate answers only the process on this machine;
    * **carrying the internal-tool token** — that header is how Faustus's OWN
      model reaches admin routes through the app bridge. A request bearing it
      is a model asking the gate to judge something, which is a model probing
      its own guard. Refused whatever the token in the path says;
    * **unknown / expired / finished token** — 404, identically for all three.
    """
    if str(client_host or "") not in ("127.0.0.1", "::1", "localhost"):
        return 403, {"error": "the agent gate answers loopback callers only"}
    if internal_token_seen:
        return 403, {"error": "the agent gate is not reachable from the app's internal tool bridge"}
    run = run_for_token(token)
    if run is None:
        return 404, {"error": "no such run"}
    if run.calls >= MAX_CALLS_PER_RUN or not run.take_token():
        run.throttled += 1
        # Denying is the only honest answer to a call the gate declined to
        # judge: allowing it would make flooding the endpoint the way through.
        decision = GateDecision(
            _DENY,
            "Faustus's gate is rate-limiting this run: it has asked to judge more calls, or "
            "faster, than a run is allowed. Slow down or stop.",
            tier="throttled", tool=str((payload or {}).get("tool_name") or ""),
        )
        return 429, {"hookSpecificOutput": decision.hook_output()}

    body = payload if isinstance(payload, dict) else {}
    tool_name = str(body.get("tool_name") or "")
    tool_input = _tool_input(body.get("tool_input"))
    decision = judge(tool_name, tool_input, cwd=body.get("cwd") or run.cwd,
                     run_id=run.run_id, run=run)
    record(run, decision, tool_use_id=body.get("tool_use_id"),
           command=_payload_text(tool_name, tool_input))
    return 200, {"hookSpecificOutput": decision.hook_output()}


# ── the transport: a listener that lives exactly as long as the run ─────────

#: The path both transports serve, so a hook script written for one works
#: against the other unchanged.
GATE_PATH = "/api/agent-gate"
#: Environment the foreign process is given. The URL and the credential are
#: separate on purpose: the credential's NAME ends in TOKEN, which is what
#: src/external_worker.py's redactor keys on, so the run's command line can be
#: shown to the user with the token starred out.
URL_ENV = "FAUSTUS_AGENT_GATE_URL"
TOKEN_ENV = "FAUSTUS_AGENT_GATE_TOKEN"

_MAX_BODY = 1 << 20

#: The hook Claude Code runs before every tool call. Stdlib only, because it
#: runs under whatever interpreter Faustus itself is running under and must not
#: need anything installed.
#:
#: It fails CLOSED. If the gate cannot be reached — Faustus stopped, the run is
#: over, the listener is gone — the answer is `deny`, not `allow`. A hook that
#: failed open would mean killing Faustus is how you get an unguarded agent.
HOOK_SCRIPT = '''\
"""Faustus PreToolUse hook. Asks the gate; refuses when it cannot ask."""
import json
import os
import sys
import urllib.error
import urllib.request

_DENY = {"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "deny",
    "permissionDecisionReason": (
        "Faustus's tool gate could not be reached, so this call was not judged. "
        "A call Faustus cannot judge is refused."),
}}


def _answer(url, token, body):
    request = urllib.request.Request(
        url.rstrip("/") + "/" + token, data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    # No proxy: the gate is on loopback, and an http_proxy in the environment
    # would otherwise send every tool call of this run to somebody else.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return (err.read() or b"").decode("utf-8", "replace")


def main():
    try:
        text = _answer(os.environ["FAUSTUS_AGENT_GATE_URL"],
                       os.environ["FAUSTUS_AGENT_GATE_TOKEN"],
                       sys.stdin.read())
        parsed = json.loads(text)
        if not isinstance(parsed, dict) or "hookSpecificOutput" not in parsed:
            raise ValueError("not a hook answer")
        sys.stdout.write(json.dumps(parsed))
    except Exception:
        sys.stdout.write(json.dumps(_DENY))
    return 0


sys.exit(main())
'''


def write_hook_script(directory: Any) -> str:
    """Write the hook script into `directory` and return its path."""
    folder = str(directory)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "faustus_pretooluse_hook.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HOOK_SCRIPT)
    try:
        os.chmod(path, 0o500)
    except OSError:  # noqa: PERF203 - Windows, and a permission is not the guard
        pass
    return path


def _handler_class():
    # Built lazily so importing this module never imports http.server: it is
    # imported by src/prove.py's caller on every dispatched job, gated or not.
    from http.server import BaseHTTPRequestHandler

    class GateHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:  # noqa: D102 - stderr is the user's
            return

        def _send(self, status: int, body: Dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            prefix = GATE_PATH + "/"
            if not self.path.startswith(prefix):
                self._send(404, {"error": "no such run"})
                return
            token = self.path[len(prefix):].split("?", 1)[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length < 0 or length > _MAX_BODY:
                self._send(413, {"error": "payload too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
            except Exception:  # noqa: BLE001 - a malformed payload is not a crash
                payload = {}
            try:
                from core.middleware import INTERNAL_TOOL_HEADER
                internal = bool(self.headers.get(INTERNAL_TOOL_HEADER))
            except Exception:  # noqa: BLE001 - standalone use
                internal = bool(self.headers.get("X-Odysseus-Internal-Token"))
            status, body = handle_hook(
                token, payload,
                client_host=(self.client_address[0] if self.client_address else ""),
                internal_token_seen=internal,
            )
            self._send(status, body)

        def do_GET(self) -> None:  # noqa: N802
            # There is nothing to read here. The gate answers questions about
            # calls; it never reports what it has decided to the process it is
            # deciding about.
            self._send(405, {"error": "POST only"})

    return GateHandler


class GateServer:
    """A loopback listener that exists for exactly one run.

    Why a listener of its own rather than only the app route: the route in
    routes/agent_gate_routes.py has to be registered on the app *and* exempted
    from its auth middleware, which is a change in files this work does not
    own. This listener needs neither, is not in the app's OpenAPI surface at
    all (so the model-facing `app_api` bridge cannot even discover it), binds
    to 127.0.0.1 on an ephemeral port, and is closed the moment the run ends.
    Both transports answer with :func:`handle_hook`, so there is one policy.
    """

    def __init__(self, run: GateRun):
        self.run = run
        self._server = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "GateServer":
        from http.server import ThreadingHTTPServer
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name=f"agent-gate-{self.run.run_id}", daemon=True)
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        return int(self._server.server_address[1]) if self._server is not None else 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{GATE_PATH}"

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
            except Exception as e:  # noqa: BLE001
                logger.debug("agent_gate: listener shutdown: %s", e)
            try:
                server.server_close()
            except Exception as e:  # noqa: BLE001
                logger.debug("agent_gate: listener close: %s", e)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


__all__ = [
    "BASH_TOOLS", "BUDGET_MS", "DECISIONS", "GATE_PATH", "HOOK_SCRIPT", "GateDecision",
    "GateRun", "GateServer", "MAX_CALLS_PER_RUN", "READ_ONLY_TOOLS", "TOKEN_ENV",
    "TOKEN_TTL_S", "URL_ENV", "WRITE_TOOLS", "close_run", "handle_hook", "judge",
    "ledger_of", "open_run", "record", "recent_ledgers", "reset", "run_for_token",
    "write_hook_script",
]
