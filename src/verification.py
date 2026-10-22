"""verification.py — VER-01/02/04/05/06: the single point where "the model
says it is done" gets checked against evidence, not against its own claim.

Nothing here replaces an existing authority — every function composes one
that already exists:

  * VER-01 `verify_criterion`/`mark_not_verifiable`/`waive_criterion`/
    `criteria_summary` add explicit, evidence-only states on top of
    `contracts.task.AcceptanceCriterion` (see the note below `CRITERION_STATES`
    for why this is a second vocabulary, not a widened one).
  * VER-02 `run_verifier` is a uniform front door onto `src.project_tests`
    (tests via `detect_test_command`/`run_tests`/`compare_with_baseline`,
    already battle-tested; lint/build/typecheck via the new
    `project_tests.detect_command`) — it runs nothing on its own.
  * VER-04 `material_proof` reconciles a task's claimed evidence through
    `src.prove.prove` (ChangeSet vs. an output_oracle-checked verification).
  * VER-05 `verify_citation` reads `src.claim_verify.verify`'s five-layer
    ladder as three words instead of a bool + layer number.

  * `verify_contract_link`/`contract_state` decide the state of a contract
    criterion (src/contract_criteria.py) from one tool call of the turn the
    model linked it to (`criteria_check`).

Stdlib only beyond the modules above. Nothing here talks to agent_loop or
Studio directly: the loop reaches `verify_contract_link` through
`contract_criteria.CriteriaBook.apply_links`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src import prove as _prove_mod
from src import project_tests
from src.claim_verify import verify as _verify_claim
from src.contracts.task import AcceptanceCriterion

# ─────────────────────────────────────────────────────────────────────────
# VER-01 — acceptance criteria with explicit, evidence-only states
# ─────────────────────────────────────────────────────────────────────────

#: Deliberately NOT `contracts.task.ACCEPTANCE_STATES` (pending/proven/unmet/
#: not_checked/not_applicable). That tuple is part of a frozen wire contract
#: this lot does not own (`AcceptanceCriterion.from_mapping` rejects anything
#: outside it, and `src/contracts/task.py` is not in this lot's file list) —
#: widening it would be an edit to a file the lot forbids touching, dressed
#: up as an addition. This is the vocabulary the lot literally asks for
#: (`pending | verified | failed | not_verifiable | waived(by, reason)`);
#: see the lot report for the two-line change to `contracts/task.py` that
#: would let `AcceptanceCriterion.state` carry it natively instead.
CRITERION_STATES = ("pending", "verified", "failed", "not_verifiable", "waived")


@dataclass(frozen=True)
class CriterionVerdict:
    """One criterion's VERIFICATION state — never the model's own checkbox.

    `state` only ever comes from one of the three functions below, each of
    which takes EVIDENCE (a pass/fail signal, a "cannot check this" reason,
    or a named human waiver). None of them has a parameter for "the model
    marked this done" — there is nothing a model's own claim could pass
    through this module that would move a criterion off `pending` by itself.
    """

    criterion_id: str
    state: str
    evidence_refs: Tuple[str, ...] = ()
    detail: str = ""
    waived_by: Optional[str] = None
    waived_reason: Optional[str] = None

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "criterion_id": self.criterion_id, "state": self.state,
            "evidence_refs": list(self.evidence_refs), "detail": self.detail,
            "waived_by": self.waived_by, "waived_reason": self.waived_reason,
        }


def _criterion_id(criterion: Any) -> str:
    if isinstance(criterion, AcceptanceCriterion):
        return criterion.id
    if isinstance(criterion, Mapping):
        return str(criterion.get("id") or "")
    return str(getattr(criterion, "id", "") or "")


def verify_criterion(criterion: Any, *, evidence_ok: Optional[bool] = None,
                     evidence_refs: Sequence[str] = (), detail: str = "") -> CriterionVerdict:
    """The only ordinary (non-waiver) way a criterion's state moves.

    `evidence_ok` must come from evidence the caller actually looked at —
    e.g. one entry of `material_proof(task)["missing"]`, or a `run_verifier`
    result — never a raw "the model says this is done" bit (this function
    has no such parameter). `None` (no evidence examined yet, or evidence
    that neither confirms nor denies) keeps the criterion `pending`: that one
    rule is the whole of the QA-19 guarantee — a checkbox the model ticks
    with nothing behind it never becomes `verified`.
    """
    cid = _criterion_id(criterion)
    if evidence_ok is None:
        return CriterionVerdict(cid, "pending", tuple(evidence_refs), detail or "no evidence examined yet")
    state = "verified" if evidence_ok else "failed"
    return CriterionVerdict(cid, state, tuple(evidence_refs), detail)


def mark_not_verifiable(criterion: Any, *, reason: str) -> CriterionVerdict:
    """The criterion cannot be checked at all (needs a human to look at a
    screenshot, a subjective judgement call, a live external system, …) —
    distinct from `pending` ("not checked YET") and from `waived` (a human
    said "skip it" for a criterion that COULD have been checked)."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("mark_not_verifiable requires a non-blank reason")
    return CriterionVerdict(_criterion_id(criterion), "not_verifiable", detail=reason)


def waive_criterion(criterion: Any, *, by: str, reason: str) -> CriterionVerdict:
    """A human explicitly decided this criterion does not need to hold —
    always named (`by`) and always explained (`reason`): a "waiver" with
    either blank is not a waiver, it is a state change with the evidence
    missing, which is exactly what this module exists to refuse."""
    by = (by or "").strip()
    reason = (reason or "").strip()
    if not by or not reason:
        raise ValueError("waive_criterion requires both a non-blank `by` and `reason`")
    return CriterionVerdict(_criterion_id(criterion), "waived",
                            waived_by=by, waived_reason=reason,
                            detail=f"waived by {by}: {reason}")


#: VER-01 verdict state -> `contracts.task.ACCEPTANCE_STATES` name, for the
#: contract criteria of a turn (src/contract_criteria.py).
_CONTRACT_STATE = {"verified": "proven", "failed": "unmet", "not_verifiable": "not_applicable",
                   "waived": "not_applicable", "pending": "pending"}


def contract_state(verdict_state: str) -> str:
    return _CONTRACT_STATE.get(str(verdict_state or ""), "pending")


def _mentions(haystack: str, token: str) -> bool:
    """`token` appears in `haystack` as a whole token: letters and digits may
    not continue it on either side (an underscore or a dot may: a test named
    `test_get_memory` mentions `get`)."""
    token = (token or "").strip().lower()
    if not token:
        return False
    import re as _re
    return _re.search(r"(?<![A-Za-z0-9])" + _re.escape(token) + r"(?![A-Za-z0-9])", haystack) is not None


def _subject_mentioned(haystack: str, subject: str) -> bool:
    subject = (subject or "").strip()
    if not subject:
        return True
    if " " not in subject:
        return _mentions(haystack, subject)
    # A case phrase ("missing argument"): any of its content words will do.
    words = [w for w in subject.lower().split() if len(w) >= 4] or subject.lower().split()
    return any(_mentions(haystack, w) for w in words)


#: Units that name the same measure, for matching a limit against an output.
#: Every byte unit is one family (a measurement in bytes, KB or GiB shows a
#: limit stated in MiB); time units are kept apart (`0.31s` is not `5 ms`).
_BYTE_UNITS = ("b", "byte", "bytes", "kb", "kib", "kilobytes", "mb", "mib", "megabytes", "gb", "gib",
               "gigabytes", "tb", "tib")
_UNIT_ALIASES = (
    _BYTE_UNITS, ("ms", "msec", "milliseconds", "millisecond"), ("us", "µs", "microseconds"),
    ("ns", "nanoseconds"), ("s", "sec", "secs", "second", "seconds", "segundo", "segundos"),
    ("min", "mins", "minute", "minutes", "minuto", "minutos"), ("h", "hr", "hrs", "hour", "hours", "hora", "horas"),
    ("%", "percent"),
)


def _unit_family(unit: str) -> Tuple[str, ...]:
    u = (unit or "").strip().lower()
    for fam in _UNIT_ALIASES:
        if u in fam:
            return fam
    if u.endswith("s") and len(u) > 3:
        return (u, u[:-1])
    return (u, u + "s")


def _limit_shown(haystack: str, limit: Mapping[str, Any]) -> bool:
    """A limit is shown by a measurement in its unit family (`31 MB` or
    `31457280 bytes` for "under 64 MiB", `12 ms` for "within 5 ms", also
    inside an identifier: `test_put_under_64mb`) or by its own number in a
    comparison (`<= 64`, `under 64`) - never by the bare digits (`5 passed` is
    not "5 ms")."""
    import re as _re
    num = str(limit.get("num") or "").strip()
    fam = [_re.escape(u) for u in _unit_family(str(limit.get("unit") or "")) if u]
    if fam and _re.search(r"(?<![A-Za-z0-9.])\d+(?:[.,]\d+)?[ _]?(?:" + "|".join(sorted(fam, key=len, reverse=True))
                          + r")(?![A-Za-z])", haystack, _re.I):
        return True
    if num and _re.search(r"(?:<=?|>=?|≤|≥|\b(?:under|below|above|over|max(?:imum)?|min(?:imum)?|limit|at most|"
                          r"at least|less than|more than|menos de|m[áa]s de|m[áa]ximo|m[íi]nimo)\b)\s*"
                          + _re.escape(num) + r"(?![\d.])", haystack, _re.I):
        return True
    return False


def _status_shown(haystack: str, status: int) -> bool:
    """An HTTP status in an output: a response line (`HTTP/1.1 404 Not
    Found`), a `status: 404`/`status_code=404` field, a curl `-w
    "%{http_code}"` line of its own, or the code followed by its reason
    phrase."""
    import re as _re
    n = str(int(status))
    return bool(_re.search(r"HTTP/\d(?:\.\d)?\s+" + n + r"\b", haystack, _re.I)
                or _re.search(r"status(?:[_ ]?code)?[\"']?\s*[:=]\s*[\"']?" + n + r"\b", haystack, _re.I)
                or _re.search(r"(?m)^\s*" + n + r"\s*$", haystack)
                or _re.search(r"(?<!\d)" + n + r"\s+[A-Z][a-z]+(?:\s[A-Z][a-z]+)*", haystack))


_TEST_ID_RE = None


def _test_ids(command: str, output: str, named: str = "") -> List[str]:
    """Test ids a call ran: `path::name` in its command or in `-v` output,
    and the test named by the link when the command or output shows it."""
    import re as _re
    global _TEST_ID_RE
    if _TEST_ID_RE is None:
        _TEST_ID_RE = _re.compile(r"[\w./\\-]*::[\w\[\]\-.:]+")
    ids = [t for t in _TEST_ID_RE.findall(command or "")]
    # `-v` lines: `path::name PASSED`. Token scan, linear in the output (a
    # regex like `\S*::\S+\s+PASSED` is quadratic on one long token).
    for line in (output or "").splitlines():
        if "::" in line and "PASSED" in line:
            parts = line.split()
            ids += [p for p, nxt in zip(parts, parts[1:]) if "::" in p and nxt.startswith("PASSED")]
    named = (named or "").strip()
    if named and (named.lower() in (command or "").lower() or named.lower() in (output or "").lower()
                  or named.split("::")[-1].lower() in (command or "").lower()):
        ids.append(named)
    return ids


import re as _re_mod
_FAILURE_WORDS = _re_mod.compile(r"\b(?:fail\w*|error\w*|reject\w*|refus\w*|invalid|falla\w*|rechaz\w*)\b", _re_mod.I)


def raw_usage(call: Mapping[str, Any]) -> bool:
    """The output is just a usage message (argparse and the like)."""
    return str(call.get("output") or "").lstrip().lower().startswith("usage:")


def verify_contract_link(criterion: Mapping[str, Any], call: Optional[Mapping[str, Any]], *,
                         last_mutation_seq: int = -1, strict: bool = True,
                         test: str = "", budget_s: float = 0.5) -> CriterionVerdict:
    """Does one tool call of the turn show this contract criterion?

    The model names the call; this decides. `evidence_ok=True` only when:
      * the call exists in this turn (`call` is not None) and is not a
        print-only command (`echo`/`printf`/`Write-Output`/`Write-Host`/a
        here-doc, also piped into `tee`/`Out-File`/..., or `python -c`/
        `node -e` that only prints literals): printing your own claim proves
        nothing; nor a help or usage call (`-h`/`--help`/`help`/`/?`, or a
        failing call whose output is only usage or argv);
      * with `strict` (setting `agent_contract_criteria_strict_evidence`):
        it ran after the turn's last change to the workspace (`seq` not
        before `last_mutation_seq`);
        its OUTPUT names the criterion's subject when it has one (a check of
        `put` is not a check of `get`) - or the subject is a command or route
        template the call actually ran (`template_ran`: not grep/ls/cat/type/
        Select-String of a file, not a help call), or the call is a passing
        targeted test run whose test id names it;
        a command or route template is only a LOCATOR: when the criterion
        names one that is not its subject, the call must have run it too.
        A template proves the whole criterion only when the rule is purely
        about the invocation form (`invocation_only`): then a successful run
        (exit 0, or the failure the sentence names) is the evidence;
        otherwise it shows at least one of the criterion's terms IN THE
        OUTPUT:
          - a key (backtick literal, quoted string, dotted/snake name);
          - an output form matcher (src/contract_criteria.py
            `_literal_matcher`): a date/instant/regex format, or a JSON shape
            with ALL its keys, or a JSON document;
          - a limit as a measurement in its unit family, or its number in a
            comparison - not bare digits;
          - the exit status it names (the call's exit code), or the HTTP
            status it names, in the response (a 200 never proves a 404 rule);
        or, with none of those, one of its content words.
        Test ids of a passing targeted run count as output for these terms.
    Matching runs on a window of the output (first 2 KB + last 8 KB) in short
    segments, with regexes from the task translated into a safe subset, under
    a per-link deadline `budget_s`; past it the link is `pending` with
    "not proven (matcher timeout)", never an exception.
    The exit code is NOT required to be 0: a criterion about an error is shown
    by a command that fails with that error. Anything short of that keeps the
    criterion `pending` with the reason in `detail` - never `failed`, which
    only the model's own admission (`verify_criterion(evidence_ok=False)`)
    produces.
    """
    if call is None:
        return verify_criterion(criterion, evidence_ok=None,
                                detail="no tool call of this turn matches that reference")
    ref = str(call.get("call_id") or "")
    from src.contract_criteria import is_echo_only, matcher_shown
    command = str(call.get("command") or "")
    if is_echo_only(str(call.get("tool") or ""), command):
        return verify_criterion(criterion, evidence_ok=None, evidence_refs=[ref],
                                detail="that call only prints text you wrote (echo/printf/Write-Output/print); "
                                       "run the program or test that shows it")
    from src.contract_criteria import is_help_call
    if is_help_call(command) or raw_usage(call):
        return verify_criterion(criterion, evidence_ok=None, evidence_refs=[ref],
                                detail="that call shows help or usage text, not the behaviour; run the "
                                       "program for real")
    if not strict:
        return verify_criterion(criterion, evidence_ok=True, evidence_refs=[ref], detail="call exists")
    seq = int(call.get("seq") or 0)
    if seq < int(last_mutation_seq):
        return verify_criterion(criterion, evidence_ok=None, evidence_refs=[ref],
                                detail="stale: the workspace changed after that call; run the check again")
    from src.contract_criteria import _window
    full_out = str(call.get("output") or "")
    # Every term search runs on the same bounded window as the form matchers
    # (first 2 KB + last 8 KB); only the linear test-id scan sees it all.
    raw_out = _window(full_out)
    out = raw_out.lower()
    located = (command + "\n" + raw_out).lower()
    passing_tests = _test_ids(command, full_out, test) if call.get("exit_code") == 0 else []
    tests_text = "\n".join(passing_tests).lower()
    import time as _time
    deadline = _time.monotonic() + float(budget_s)
    timed_out = False

    def shown(m: Mapping[str, Any]) -> bool:
        nonlocal timed_out
        r = matcher_shown(dict(m), output=raw_out, command=command, deadline=deadline)
        if r is None:
            timed_out = True
        return bool(r)

    # Command and route templates only LOCATE the call: it must have run the
    # program with that invocation (not grep/ls/cat of it, not --help).
    templates = [m for m in (criterion.get("matchers") or []) if isinstance(m, Mapping) and m.get("on") == "command"]
    subject = str(criterion.get("subject") or "")
    subject_matcher = criterion.get("subject_matcher")
    subject_is_template = isinstance(subject_matcher, Mapping) and subject_matcher.get("on") == "command"
    if subject:
        if isinstance(subject_matcher, Mapping):
            subject_ok = shown(subject_matcher)
        else:
            # In the output, in the id of a passing targeted test, or - for a
            # one-word subject - as an argument of the command that ran
            # (`python -m vault get ...` is a check of `get`; its terms must
            # still show in the output).
            args = command.split(None, 1)[1].lower() if len(command.split(None, 1)) > 1 else ""
            subject_ok = (_subject_mentioned(out, subject)
                          or bool(tests_text and _subject_mentioned(tests_text, subject))
                          or (" " not in subject.strip() and _mentions(args, subject.lower())))
        if not subject_ok:
            return verify_criterion(criterion, evidence_ok=None, evidence_refs=[ref],
                                    detail=("that call did not run `" + subject + "` (a help, usage, grep or "
                                            "listing call does not count)") if subject_is_template else
                                    f"the output of that call does not mention `{subject}`; "
                                    "check this subject on its own and print it")
    if templates and not subject_is_template and not any(shown(m) for m in templates):
        return verify_criterion(criterion, evidence_ok=None, evidence_refs=[ref],
                                detail="that call did not run the invocation the criterion names ("
                                       + ", ".join(str(m.get("src")) for m in templates[:3]) + ")")
    if test and not (_mentions(located, test.lower()) or test.lower() in located
                     or test.split("::")[-1].lower() in located):
        return verify_criterion(criterion, evidence_ok=None, evidence_refs=[ref],
                                detail=f"that call does not run or show `{test}`")
    from src.contract_criteria import invocation_only
    if invocation_only(dict(criterion)):
        # Nothing but an invocation form: running it is the proof - and it
        # must succeed, unless the sentence is about a failure.
        if call.get("exit_code") not in (0, None) and not _FAILURE_WORDS.search(str(criterion.get("quote") or "")):
            return verify_criterion(criterion, evidence_ok=None, evidence_refs=[ref],
                                    detail="that invocation failed (exit "
                                           f"{call.get('exit_code')}); the form is shown by a successful run")
        return verify_criterion(criterion, evidence_ok=True, evidence_refs=[ref], detail="shown by " + ref)
    # Every other term must show in the OUTPUT.
    hay = out + ("\n" + tests_text if tests_text else "")
    keys = [str(k) for k in (criterion.get("keys") or []) if str(k).strip() and str(k) != subject]
    out_matchers = [m for m in (criterion.get("matchers") or []) if isinstance(m, Mapping) and m.get("on") == "output"]
    limits = [l for l in (criterion.get("limits") or []) if isinstance(l, Mapping)]
    exits = [x for x in (criterion.get("exits") or []) if isinstance(x, int)]
    statuses = [x for x in (criterion.get("statuses") or []) if isinstance(x, int)]
    words = [str(w) for w in (criterion.get("words") or []) if str(w).strip()]
    if keys or out_matchers or limits or exits or statuses:
        ok = (any(_mentions(hay, k) or (not k.isalnum() and k.lower() in hay) for k in keys)
              or any(shown(m) for m in out_matchers)
              or any(_limit_shown(hay, l) for l in limits)
              or (call.get("exit_code") in exits)
              or any(_status_shown(raw_out, s) for s in statuses))
        shown_terms = ", ".join(keys[:3] + [str(m.get("src")) for m in out_matchers[:2]]
                                + [f"{l.get('num')} {l.get('unit')}" for l in limits[:2]]
                                + [f"exit {x}" for x in exits[:2]] + [f"HTTP {s}" for s in statuses[:2]])
    else:
        ok = not words or any(_mentions(hay, w) for w in words)
        shown_terms = ", ".join(words[:6])
    if not ok:
        if timed_out:
            return verify_criterion(criterion, evidence_ok=None, evidence_refs=[ref],
                                    detail="not proven (matcher timeout)")
        return verify_criterion(criterion, evidence_ok=None, evidence_refs=[ref],
                                detail=f"the output of that call shows none of the criterion's terms ({shown_terms})")
    return verify_criterion(criterion, evidence_ok=True, evidence_refs=[ref], detail="shown by " + ref)


def criteria_summary(verdicts: Iterable[CriterionVerdict]) -> Dict[str, Any]:
    """Counts per state, and whether a task carrying these criteria may reach
    `succeeded` (contracts.task.TASK_STATES). VER-01's literal ask —
    "bloquea succeeded mientras haya alguno pendiente" — so only `pending`
    blocks; a `failed` or `not_verifiable` criterion is the caller's problem
    to resolve (retry, waive it explicitly, or settle for `partial`), not
    something this function papers over."""
    rows = list(verdicts)
    counts = {s: 0 for s in CRITERION_STATES}
    for v in rows:
        counts[v.state] = counts.get(v.state, 0) + 1
    return {
        "total": len(rows),
        **counts,
        "all_settled": counts["pending"] == 0,
        "blocks_succeeded": counts["pending"] > 0,
    }


# ─────────────────────────────────────────────────────────────────────────
# VER-02 — deterministic verifiers with a before/after baseline
# ─────────────────────────────────────────────────────────────────────────

def run_verifier(kind: str, workspace: str, *, checkpoint_sha: Optional[str] = None,
                 changed: Optional[Iterable[str]] = None, override: Optional[str] = None,
                 timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """Run `kind` ("tests" | "lint" | "build" | "typecheck") after the change
    and — when `checkpoint_sha` names a pre-change tree — again before it, so
    a caller can tell a failure the patch INTRODUCED from one that predates
    it (QA-20). One uniform shape for all four kinds::

        {"kind", "ran", "ok", "inconclusive", "command", "summary",
         "baseline": {"ran", "ok", "failed": [...], "summary"} | None,
         "after":    {"ran", "ok", "failed": [...], "summary"},
         "new_failures": [...], "fixed": [...], "preexisting": [...]}

    For "tests" this is `src.project_tests` end to end — detection, the
    scoped pytest run, `compare_with_baseline` — reshaped into the above; the
    itemised failure identity (`test_file::test_name`) it already gets right
    is unchanged, and so is the constraint it inherits: the baseline diff
    only runs when `changed` names files pytest can scope to (see that
    function's docstring). For lint/build/typecheck there is usually no
    itemised failure list to diff (parsing every linter's own output format
    is out of scope here) — the baseline diff falls back to ONE synthetic id
    for the whole command, which still correctly tells "this command was
    already red at the checkpoint" from "this command just went red": the
    exact distinction QA-20 asks for, at a coarser grain.

    Never raises: an unknown `kind` or a command that cannot be detected
    comes back `ran=False, inconclusive=True` with the reason in `summary` —
    "could not verify", never a silent pass.
    """
    changed = list(changed or [])
    out: Dict[str, Any] = {
        "kind": kind, "ran": False, "ok": None, "inconclusive": True,
        "command": "", "summary": "", "baseline": None, "after": None,
        "new_failures": [], "fixed": [], "preexisting": [],
    }
    try:
        spec = project_tests.detect_command(kind, workspace, override)
    except ValueError as e:
        out["summary"] = str(e)
        return out
    if spec is None:
        out["summary"] = f"no {kind} command detected for this project"
        return out

    if kind == "tests":
        after = project_tests.run_tests(workspace, spec, changed=changed, scope="related", timeout_s=timeout_s)
    else:
        after = project_tests.run_tests(workspace, spec, timeout_s=timeout_s)
    out.update(ran=bool(after.get("ran")), ok=after.get("ok"),
               inconclusive=bool(after.get("inconclusive")),
               command=after.get("command") or "", summary=after.get("summary") or "")
    out["after"] = {"ran": after.get("ran"), "ok": after.get("ok"),
                    "failed": list(after.get("failures") or []), "summary": after.get("summary")}

    if not checkpoint_sha or not after.get("ran"):
        # No baseline possible: every after-failure stays unclassified rather
        # than being silently called "new" with nothing to compare it to.
        out["new_failures"] = list(after.get("failures") or [])
        return out

    if kind == "tests":
        compared = project_tests.compare_with_baseline(workspace, checkpoint_sha, spec, dict(after), changed=changed)
        base = compared.get("baseline") or {}
        out["baseline"] = {"ran": base.get("ran"), "ok": base.get("ok"),
                           "failed": list(base.get("failures") or []), "summary": base.get("summary")}
        out["new_failures"] = list(compared.get("new_failures") or [])
        out["preexisting"] = list(compared.get("pre_existing") or [])
        out["fixed"] = list(compared.get("fixed") or [])
        return out

    baseline = _run_baseline_tree(workspace, checkpoint_sha, spec, timeout_s=timeout_s)
    if baseline is None:
        out["new_failures"] = list(after.get("failures") or [])
        return out
    out["baseline"] = {"ran": baseline.get("ran"), "ok": baseline.get("ok"),
                       "failed": list(baseline.get("failures") or []), "summary": baseline.get("summary")}
    failed_before = bool(baseline.get("ran")) and baseline.get("ok") is False and not baseline.get("inconclusive")
    failed_after = bool(after.get("ran")) and after.get("ok") is False and not after.get("inconclusive")
    marker = f"{kind}: {spec.get('label') or spec.get('command') or kind}"
    if failed_after and failed_before:
        out["preexisting"] = [marker]
    elif failed_after and not failed_before:
        out["new_failures"] = [marker]
    elif failed_before and not failed_after:
        out["fixed"] = [marker]
    return out


def _run_baseline_tree(workspace: str, checkpoint_sha: str, spec: Dict[str, Any], *,
                       timeout_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Export the checkpoint tree and run `spec` there. None when the export
    itself fails (no git, unknown sha, …) — the caller then leaves every
    after-failure unclassified instead of guessing what the checkpoint held."""
    import shutil as _shutil
    import tempfile
    try:
        from src import workspace_checkpoints as wc
    except Exception:  # pragma: no cover - defensive, matches project_tests style
        return None
    tmp = tempfile.mkdtemp(prefix="odysseus-verifier-")
    try:
        if not wc.export_tree(workspace, checkpoint_sha, tmp):
            return None
        return project_tests.run_tests(tmp, dict(spec), timeout_s=timeout_s)
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────
# VER-04 — material proof, not just a green exit code
# ─────────────────────────────────────────────────────────────────────────

def _as_list(criteria: Any) -> List[Any]:
    if criteria is None:
        return []
    if isinstance(criteria, Mapping):
        return list(criteria.values())
    if isinstance(criteria, (str, bytes)):
        return []
    try:
        return list(criteria)
    except TypeError:
        return []


def _criterion_evidence_refs(criterion: Any) -> List[str]:
    if isinstance(criterion, AcceptanceCriterion):
        return list(criterion.evidence_refs)
    if isinstance(criterion, Mapping):
        return [str(r) for r in (criterion.get("evidence_refs") or []) if r]
    return [str(r) for r in (getattr(criterion, "evidence_refs", None) or []) if r]


def material_proof(task: Any) -> Dict[str, Any]:
    """VER-04: does a "done" task have MATERIAL evidence behind it, per
    criterion — not just a verification that ran and passed.

    `task` is a mapping (or an object with matching attributes) carrying:
      - `acceptance_criteria`: an iterable of `AcceptanceCriterion` or
        `{"id", "evidence_refs": [...]}` mappings;
      - `evidence`: the ChangeSet-shaped mapping `src.prove` reads (added /
        modified / deleted / checkpoint / source) — what a checkpoint diff
        (or the harness's mtime snapshot) actually saw change on disk;
      - `verification` (optional): an output_oracle-checked verifier result
        — typically what `run_verifier`/`project_tests.run_tests` returned,
        so a bare exit 0 has already been downgraded to
        `output_oracle.EXIT_OUTPUT_MISMATCH` wherever it lied about the
        declared output before this ever sees it.

    A criterion counts as materially proved only when it names at least one
    `evidence_refs` path AND `src.prove.prove` reconciles that path against
    the observed changes as `"proved"`. This is deliberately the QA-19
    answer at the single-criterion level: tests green with NO evidence_ref
    recorded for a criterion is exactly a criterion with empty
    `evidence_refs` — the tests passing never proves it by itself, so it
    lands in `missing` and `ok` is False (the caller keeps the task
    `partial`, never `succeeded`).

    Returns `{"ok": bool, "missing": [criterion_id, ...],
    "proofs": {criterion_id: <src.prove.prove() packet>}}`. Never raises:
    `src.prove.prove` is itself total, and a criterion this function cannot
    read evidence for is simply `missing`, not a crash.
    """
    if isinstance(task, Mapping):
        criteria = _as_list(task.get("acceptance_criteria"))
        evidence = task.get("evidence")
        verification = task.get("verification")
    else:
        criteria = _as_list(getattr(task, "acceptance_criteria", None))
        evidence = getattr(task, "evidence", None)
        verification = getattr(task, "verification", None)

    missing: List[str] = []
    proofs: Dict[str, Any] = {}
    for c in criteria:
        cid = _criterion_id(c)
        refs = _criterion_evidence_refs(c)
        if not refs:
            missing.append(cid)
            continue
        proof = _prove_mod.prove(evidence, verification, {"paths": refs})
        proofs[cid] = proof
        if proof.get("verdict") != "proved":
            missing.append(cid)
    return {"ok": bool(criteria) and not missing, "missing": missing, "proofs": proofs}


# ─────────────────────────────────────────────────────────────────────────
# VER-05 — provenance a citation must earn, not just wear
# ─────────────────────────────────────────────────────────────────────────

def verify_citation(sentence: Any, source_text: Any, *, judge: Any = None) -> str:
    """Léxico + solapamiento, sin modelo por defecto (an injected `judge` is
    passed straight through to `claim_verify.verify` for the rare caller
    that also wants its layer 5). Returns one of "supported" | "weak" |
    "unsupported".

    Built directly on `claim_verify.verify`'s five-layer ladder, read as
    three words instead of a bool + layer number:

      - **"supported"**    — `verify()["supported"]` is True (layers 1-3, or
        a judge that said yes at layer 5).
      - **"unsupported"**  — an EXPLICIT contradiction: layer 4 (the source
        is missing a figure or name the sentence states) or a judge that
        said no at layer 5. This is QA-21's case: an accessible source that
        simply does not contain the cited figure is "unsupported", never a
        stamp of a verified report.
      - **"weak"**         — nothing settled it either way (`layer` is
        `None`): the claim is plausible but no deterministic layer, and no
        judge, could confirm or refute it. "weak" is this function's word
        for exactly the case `claim_verify`'s own docstring warns against
        rounding up to "supported".

    `src.research_citations` keeps its own, more permissive
    `VERDICT_SUPPORTED/REFUTED/UNCHECKED` mapping for the citation-legend UX
    (a sentence with no figures at all reads as "sin comprobar", not
    "weak") — this function does not replace it, it is the plain
    three-tier primitive VER-05 asks for, usable directly on a
    `(sentence, source_text)` pair without a report or a source registry.
    """
    result = _verify_claim(sentence, source_text, judge=judge)
    if result.get("supported"):
        return "supported"
    if result.get("layer") in (4, 5):
        return "unsupported"
    return "weak"
