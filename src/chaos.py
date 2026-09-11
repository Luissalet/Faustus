"""chaos.py — EVAL-03: one named, unified catalogue of adversarial fixtures.

Faustus's chaos coverage was real but scattered: a disk-full case lived in
`tests/eval/test_chaos_protocols.py`, a real disk-headroom mechanism in
`src/disk_ballast.py`, retry/timeout discipline in `src/retry_policy.py`, an
MCP transport failure in `src/mcp_manager.py`, a process-tree kill in
`src/agent_tools/subprocess_tools.py`/`src/process_ownership.py` — each
correct on its own, with no single place that named the fixtures or let
anyone ask "what would this one do?" without reading four modules first.

This module does not re-implement any of that. It is a **named index** onto
the real mechanism each fixture already exercises, plus a `dry_run` that
describes — never performs — the injection: `POST /api/ops/chaos/{fixture}`
(routes/ops_routes.py) is dry-run-only on purpose. Actually RUNNING a fixture
against a live process is what the existing pytest files already do, safely,
inside a disposable process; a "reproduce this fixture" button that really
injects ENOSPC or kills a tree in the running server is UI another lot wires,
against these same descriptions, once someone decides where such a button is
safe to click from.

The one invariant every fixture in this catalogue names (EVAL-03's
acceptance): **no injected failure reads back as a false success, and no
state already held is silently discarded.**
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict


@dataclass(frozen=True)
class ChaosFixture:
    name: str
    summary: str
    would_inject: str
    expected_result: str
    real_module: str
    verified_by: str


FIXTURES: Dict[str, ChaosFixture] = {
    "disk_full": ChaosFixture(
        name="disk_full",
        summary=(
            "The disk fills up while Faustus is mid-write — an artifact "
            "publish, a checkpoint, disk_ballast's own preallocation."
        ),
        would_inject=(
            "The next os.fsync()/os.write() inside the guarded section raises "
            "OSError(errno.ENOSPC, 'No space left on device')."
        ),
        expected_result=(
            "The write raises rather than linking or reporting a truncated "
            "result as done (src.artifact_store.publish_copy); no half-written "
            "blob is left under a name a reader could find. "
            "src.disk_ballast's preallocated ballast files exist for exactly "
            "this moment — unlinking one is instantaneous and cannot itself "
            "fail for lack of space, buying the minutes a real cleanup needs."
        ),
        real_module="src.artifact_store.publish_copy, src.disk_ballast",
        verified_by=(
            "tests/eval/test_chaos_protocols.py::"
            "test_disk_full_while_publishing_an_artifact_never_reports_success"
        ),
    ),
    "model_timeout": ChaosFixture(
        name="model_timeout",
        summary=(
            "The model/provider stalls, or answers with a Retry-After long "
            "enough to stall a retry loop for years."
        ),
        would_inject=(
            "A streaming response that stops producing tokens, or a 429 with "
            "an absurd Retry-After header (e.g. 99999999s)."
        ),
        expected_result=(
            "src.retry_policy.delay clamps any Retry-After to the caller's "
            "own cap rather than honouring it verbatim, and the call is "
            "classified retry/expected_error — never silently reported as a "
            "completed turn."
        ),
        real_module="src.retry_policy",
        verified_by=(
            "tests/eval/test_chaos_protocols.py::"
            "test_an_absurd_retry_after_is_capped_not_honoured_verbatim"
        ),
    ),
    "mcp_drop": ChaosFixture(
        name="mcp_drop",
        summary=(
            "An MCP server's connection drops, or answers with bytes that do "
            "not parse as its protocol, mid-call."
        ),
        would_inject=(
            "McpManager._do_call raises — a transport error, or a "
            "JSONDecodeError on garbage bytes from the peer."
        ),
        expected_result=(
            "McpManager.call_tool returns {'error': ..., 'exit_code': 1} — "
            "never a dict shaped like a completed tool result the agent loop "
            "would read as success (no 'stdout' key masquerading as real "
            "output)."
        ),
        real_module="src.mcp_manager.McpManager.call_tool",
        verified_by=(
            "tests/eval/test_chaos_protocols.py::"
            "test_mcp_garbage_response_returns_an_error_not_a_false_success"
        ),
    ),
    "process_kill": ChaosFixture(
        name="process_kill",
        summary=(
            "A spawned command's process tree is killed out from under it — "
            "the idle watchdog, a turn cancellation, an operator's Stop."
        ),
        would_inject=(
            "SIGKILL / taskkill against the process tree recorded for this "
            "pid, sent the instant the idle-output budget or the hard "
            "timeout expires."
        ),
        expected_result=(
            "src.agent_tools.subprocess_tools._kill_tree only ever signals a "
            "tree this process is still holding the live process object for "
            "(src.process_ownership.check verifies ownership first); a pid "
            "the OS already recycled to something else — plausibly the "
            "Ollama server with models resident — is refused, not killed, "
            "and the refusal is logged rather than silently swallowed."
        ),
        real_module=(
            "src.agent_tools.subprocess_tools._kill_tree, src.process_ownership"
        ),
        verified_by=(
            "tests/test_subprocess_hardening.py::"
            "test_idle_watchdog_kills_a_silent_command_and_its_children"
        ),
    ),
}


def list_fixtures() -> Dict[str, Dict[str, str]]:
    """Every fixture's full description, keyed by name."""
    return {name: asdict(f) for name, f in FIXTURES.items()}


def dry_run(fixture: str) -> Dict[str, Any]:
    """What `POST /api/ops/chaos/{fixture}` answers.

    Never injects anything — this is the fixture's description, for a person
    (or a future "reproduce this fixture" UI) deciding whether to run its
    real pytest. Raises `KeyError` for an unknown fixture name; the route
    turns that into a 404 naming the available set.
    """
    fixture = str(fixture or "").strip()
    if fixture not in FIXTURES:
        raise KeyError(fixture)
    f = FIXTURES[fixture]
    out = asdict(f)
    out["fixture"] = out.pop("name")
    out["dry_run"] = True
    return out
