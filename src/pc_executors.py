"""The command-line agents of the person's PC, for a Faustus that lives on a Spark.

``src.agent_runners`` knows how to invoke Claude Code, Codex and the rest, but it only finds them on the machine Faustus
runs on. On a Spark they are not there: they live on the PC, with their sign-in. The PC Hub, reached through the link
tunnel (:mod:`src.hub_link`), starts programs on that PC (its ``executors`` facet), so this module

* asks the Hub which of the runners' programs the PC has (:func:`installed`), so a list can say where each one lives;
* runs one task there (:func:`run_task`): the command line comes from the same runner table, the Hub starts it, and the
  output is read in pieces until it ends, with the same result shape as :func:`src.external_worker.run_task`.

A run on the PC is not gated by Faustus (its hook scripts are on this machine, not there) and has no resumable session:
every turn starts the agent again with a summary of the thread. Nothing here raises; failures come back as a result with
``error`` set.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from src import agent_runners as reg
from src import hoard_hub, hub_link

CACHE_S = 30.0
FAILED_CACHE_S = 10.0
POLL_S = 1.0
MAX_POLL_FAILURES = 5
_cache: Dict[str, Any] = {"at": 0.0, "ttl": 0.0, "value": None, "names": ()}


def available() -> bool:
    """True when Faustus reaches a Hub on another machine (the link file is set)."""
    return hub_link.link() is not None


def _programs(runners: List[Any]) -> List[str]:
    return sorted({str(r.argv[0]) for r in runners if r.kind == "cli" and r.argv})


def installed(*, refresh: bool = False) -> Dict[str, Any]:
    """``{"host", "programs": {name: path}}`` for the PC's executables, ``{}`` programs when the Hub does not answer or
    there is no link. Cached for a few seconds: the thread's panel asks on every render."""
    linked = hub_link.link()
    if not linked:
        return {"host": "", "programs": {}, "reachable": False}
    names = tuple(_programs(reg.runners()))
    now = time.monotonic()
    if (not refresh and _cache["value"] is not None and _cache["names"] == names
            and now - _cache["at"] < _cache["ttl"]):
        return _cache["value"]
    status, body, _ = hoard_hub._call("GET", "/api/executors?names=" + quote(",".join(names), safe=","),
                                      timeout=6, where=linked)
    if status == 200 and isinstance(body, dict) and isinstance(body.get("executors"), dict):
        value = {"host": str(body.get("host") or ""), "reachable": True,
                 "programs": {n: str(v.get("path") or "") for n, v in body["executors"].items()
                              if isinstance(v, dict) and v.get("installed")}}
        ttl = CACHE_S
    else:
        value = {"host": "", "programs": {}, "reachable": False,
                 "error": "the PC Hub does not answer" if status is None else f"the PC Hub answered {status}"}
        ttl = FAILED_CACHE_S
    _cache.update(at=now, ttl=ttl, value=value, names=names)
    return value


def reset_cache() -> None:
    _cache.update(at=0.0, ttl=0.0, value=None, names=())


def has(runner: Any) -> bool:
    """Does the PC have this runner's program?"""
    return bool(runner is not None and runner.argv and str(runner.argv[0]) in installed()["programs"])


def _failure(key: str, reason: str, **extra: Any) -> Dict[str, Any]:
    out = {"ok": False, "exit_code": None, "outcome": "expected_error", "output_tail": "", "response_text": "",
           "seconds": 0.0, "runner": key, "error": reason, "cancelled": False, "where": "pc"}
    out.update(extra)
    return out


def run_task(runner_key: Any, task: str, *, workspace: Optional[str] = None, model: Optional[str] = None,
             timeout_s: Optional[float] = None, should_cancel: Optional[Callable[[], bool]] = None,
             on_output: Optional[Callable[[str], None]] = None, poll_s: float = POLL_S) -> Dict[str, Any]:
    """Run ONE task with a runner on the PC. ``workspace`` is a folder on the PC (empty: the Hub gives the run its own)."""
    started = time.time()
    runner = runner_key if isinstance(runner_key, reg.Runner) else reg.get(runner_key)
    key = runner.key if runner is not None else str(runner_key or "")
    linked = hub_link.link()
    if runner is None or runner.kind != "cli" or not runner.argv:
        return _failure(key, f"unknown agent runner: {runner_key!r}")
    if not linked:
        return _failure(key, "this Faustus is not linked to a PC Hub")
    if not reg.enabled():
        return _failure(key, "external agent runners are off: turn on Settings → Agent & automation → "
                             "`agent_external_runners`")
    for field, value in (("model", model),):
        if value is not None and (not isinstance(value, str) or value.lstrip().startswith("-")
                                  or any(c in value for c in ("\n", "\r", "\0"))):
            return _failure(key, f"Invalid {field}: an option or control character is not an identifier")
    folder = str(workspace or "").strip() or None
    argv = reg.build_argv(runner, str(task or ""), model=model, cwd=folder)
    if not argv:
        return _failure(key, f"{runner.label}: the table produced an empty command for this task")
    limit = float(timeout_s if timeout_s is not None else reg.timeout_s())
    body = {"argv": argv, "stdin": str(task or "") if runner.stdin_task else None, "cwd": folder,
            "env": reg.table_env(runner, model=model, cwd=folder), "timeout_s": limit, "label": runner.label}
    code, answer = hub_link.post("/api/executors/runs", body, timeout=30)
    if code != 200 or not isinstance(answer, dict) or not answer.get("id"):
        why = (answer or {}).get("error") if isinstance(answer, dict) else ""
        return _failure(key, str(why or "the PC Hub did not start the run" if code is not None
                                 else "the PC Hub does not answer"), http_status=code)
    run_id, host = str(answer["id"]), str(answer.get("host") or "")
    path = "/api/executors/runs/" + quote(run_id, safe="")
    offset, parts, failures, cancelled_at = 0, [], 0, None
    deadline = time.monotonic() + limit + 60
    state: Dict[str, Any] = {}
    while True:
        if should_cancel is not None and cancelled_at is None and should_cancel():
            cancelled_at = time.monotonic()
            hub_link.post(path + "/cancel", {}, timeout=10)
        code2, state, _ = hoard_hub._call("GET", f"{path}?since={offset}", timeout=15, where=linked)
        if code2 == 200 and isinstance(state, dict):
            failures = 0
            chunk = str(state.get("output") or "")
            if chunk:
                parts.append(chunk)
                if on_output is not None:
                    try:
                        on_output(chunk)
                    except Exception:  # noqa: BLE001 - a callback never breaks the run
                        pass
            offset = int(state.get("offset") or offset)
            if state.get("status") != "running" and not state.get("more"):
                break
        else:
            failures += 1
            if failures >= MAX_POLL_FAILURES:
                return _failure(key, "the PC Hub stopped answering while the agent was running; "
                                     "it may still be working there", run_id=run_id, host=host,
                                output_tail="".join(parts)[-8000:], seconds=round(time.time() - started, 2))
        if time.monotonic() > deadline:
            hub_link.post(path + "/cancel", {}, timeout=10)
            return _failure(key, "the PC Hub never reported the end of the run", run_id=run_id, host=host,
                            output_tail="".join(parts)[-8000:], seconds=round(time.time() - started, 2))
        time.sleep(poll_s)
    text = "".join(parts).strip()
    status = str(state.get("status") or "")
    ok = status == "done"
    error = ""
    if status == "timeout":
        error = f"{runner.label} was stopped after {int(limit)}s on the PC"
    elif status == "error":
        error = f"{runner.label} exited with code {state.get('exit_code')} on the PC"
    return {"ok": ok, "exit_code": state.get("exit_code"), "outcome": "ok" if ok else
            ("cancelled" if status == "cancelled" else "expected_error"), "output_tail": text[-8000:],
            "response_text": text, "seconds": round(time.time() - started, 2), "runner": key, "error": error,
            "cancelled": status == "cancelled", "where": "pc", "host": host, "run_id": run_id,
            "unguarded": True}
