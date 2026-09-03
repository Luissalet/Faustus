"""prove.py — the step after `observe`: what can actually be shown, and what
cannot.

    "a mutation is not the completion of the objective"

A transport's ack is not evidence. "The command ran" is not evidence. A worker
saying it wrote three files is not evidence. Faustus already does
``prepare → revalidate → commit → observe`` around a dispatched job
(src/dispatch.py: a checkpoint before, the diff after, `claimed_only` for what
a worker said and did not do). This module adds the missing step: one canonical
packet that reconciles the observation with the claims and says, explicitly,
*how much of it is proved and why the rest is not*.

    prove(evidence, verification, claims) -> {
        "verdict": "proved" | "partial" | "unproved" | "contradicted",
        "confidence": 0..1,
        "uncertainty": [{"kind", "detail"}],   # never empty below 1.0
        "observations": [{"kind", "detail"}],
        "identity": "<sha256>",
        "schema_version": 1,
        "at": <clock>,
    }

The four verdicts, and the discipline behind each:

``proved``
    the verification ran and PASSED and every claimed path is in the observed
    changes. Nothing else earns this word.
``partial``
    changes were observed but something is unaccounted for — a claim with no
    change behind it, a verification that could not decide, a worker that did
    not finish.
``unproved``
    **no verification runner and nothing observable changed**: the work may
    have happened and nothing here can show it. This is NOT a failure and NOT
    an error; it is the honest answer, and it is a value of its own precisely
    so that a caller cannot round it down to "failed" or up to "done".
``contradicted``
    the verification failed, or the disk contradicts a claim — an exact
    checkpoint diff that does not contain a path a worker says it wrote, or
    contains it under the opposite kind (claimed a deletion, the file is
    there).

Two rules the packet keeps:

* **`uncertainty` is never empty while `confidence < 1`.** Every reason the
  confidence dropped is a named entry with a human detail: no test runner, a
  checkpoint that could not be taken, an mtime-only fallback, a truncated
  change list, a cancelled worker. A caller must be able to read *why* the
  number is not 1 without reverse-engineering it.
* **`identity` length-prefixes every variable-length field before
  concatenating it.** Without the prefix ``["ab", "c"]`` and ``["a", "bc"]``
  hash the same; with it they cannot. Lists are sorted and de-duplicated
  first, so the transport's ordering — or a page boundary that repeats a row —
  cannot change the identity of the same evidence.

One thing a proof cannot derive for itself has its own entry point:
:func:`note_external_gate`. Whether the agents that did the work were ones
Faustus wrote — and, when they were not, whether Faustus's own guard judged
their tool calls before they ran (src/agent_gate.py) — is knowledge the
dispatch path holds and this module does not. It has three honest answers, and
the middle one is the reason the function exists: a run that was gated with
something left unjudged keeps a NARROWER uncertainty rather than losing the
entry entirely.

Pure, stdlib-only, injectable clock, and total: every entry point returns a
Proof, never an exception. An internal failure answers `unproved` with an
`internal_error` uncertainty, because a module that cannot read its own inputs
knows nothing — it does not get to say "proved".
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

# ── the named reasons a confidence is not 1 ─────────────────────────────────
NO_VERIFICATION_RUNNER = "no_verification_runner"
VERIFICATION_INCONCLUSIVE = "verification_inconclusive"
VERIFICATION_FAILED = "verification_failed"
PRE_EXISTING_FAILURES = "pre_existing_failures"
NO_CHECKPOINT = "no_checkpoint"
MTIME_ONLY = "mtime_only"
TRUNCATED_CHANGES = "truncated_changes"
CLAIMS_UNACCOUNTED = "claims_unaccounted"
CLAIM_NOT_ON_DISK = "claim_not_on_disk"
CLAIM_KIND_MISMATCH = "claim_kind_mismatch"
WORKER_CANCELLED = "worker_cancelled"
WORKER_UNFINISHED = "worker_unfinished"
NO_OBSERVABLE_CHANGE = "no_observable_change"
INTERNAL_ERROR = "internal_error"
#: An agent Faustus did not write ran its own shell and nothing judged it.
EXTERNAL_UNGUARDED = "external_agent_unguarded"
#: The narrower one: that agent's calls WERE judged, except for these.
EXTERNAL_TOOLS_UNJUDGED = "external_agent_tools_unjudged"

#: How much each named uncertainty costs the confidence. They are subtracted
#: from 1.0 and then capped by the verdict's own ceiling.
PENALTY: Dict[str, float] = {
    VERIFICATION_FAILED: 0.90,
    CLAIM_NOT_ON_DISK: 0.80,
    CLAIM_KIND_MISMATCH: 0.80,
    INTERNAL_ERROR: 1.00,
    NO_VERIFICATION_RUNNER: 0.35,
    VERIFICATION_INCONCLUSIVE: 0.30,
    NO_CHECKPOINT: 0.30,
    WORKER_UNFINISHED: 0.30,
    CLAIMS_UNACCOUNTED: 0.25,
    NO_OBSERVABLE_CHANGE: 0.25,
    WORKER_CANCELLED: 0.20,
    TRUNCATED_CHANGES: 0.15,
    PRE_EXISTING_FAILURES: 0.10,
    MTIME_ONLY: 0.10,
    EXTERNAL_UNGUARDED: 0.10,
    # Deliberately lighter than the blanket entry above. A gated run whose gate
    # met three tools it had never heard of is in a different position from one
    # nothing looked at, and a proof that priced them the same would give a
    # reader no reason to prefer the gate.
    EXTERNAL_TOOLS_UNJUDGED: 0.05,
}

#: The most confidence each verdict may carry, whatever the penalties say.
CEILING: Dict[str, float] = {"proved": 1.0, "partial": 0.70, "unproved": 0.35, "contradicted": 0.05}

VERDICTS: Tuple[str, ...] = ("proved", "partial", "unproved", "contradicted")

#: Statuses a worker may end in without holding the proof back.
_FINISHED_OK = frozenset({"done", "complete", "ok", "success"})
#: Statuses that mean somebody stopped it (four-value outcomes: NOT a failure,
#: but the objective is not finished either).
_CANCELLED = frozenset({"cancelled", "canceled", "stopped", "stopped_by_user", "cancelling", "interrupted"})

Proof = Dict[str, Any]


# ── normalisation (total: every helper survives any input) ──────────────────

def _text(value: Any) -> str:
    try:
        return "" if value is None else str(value)
    except Exception:  # noqa: BLE001 - a normaliser never raises
        return ""


def _path(value: Any) -> str:
    """A path as the evidence and the claims can be compared: forward slashes,
    no leading or trailing separator, whitespace stripped."""
    return _text(value).replace("\\", "/").strip().strip("/")


def _paths(values: Any) -> List[str]:
    out: List[str] = []
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return out
    for v in values:
        p = _path(v)
        if p and p not in out:
            out.append(p)
    return out


def _same_path(a: str, b: str) -> bool:
    """The comparison src/dispatch.py already uses for `claimed_only`: equal,
    or one is the other's tail (a worker names `cart.py`, the diff names
    `src/cart.py`)."""
    x, y = a.lower(), b.lower()
    return x == y or x.endswith("/" + y) or y.endswith("/" + x)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if value != value:                                  # NaN
        return lo
    return lo if value < lo else (hi if value > hi else value)


# ── the identity hash (length-prefixed, order-free) ─────────────────────────

def _lp(raw: bytes) -> bytes:
    """``<len>:<bytes>``. The anti-collision rule: a variable-length field is
    never concatenated without its own length in front of it."""
    return str(len(raw)).encode("ascii") + b":" + raw


def _encode(value: Any) -> bytes:
    if value is None:
        return b"~"
    if isinstance(value, bool):
        return b"1" if value else b"0"
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted({_text(v) for v in value})       # order-free, page-free
        return _lp(str(len(items)).encode("ascii")) + b"".join(_lp(i.encode("utf-8", "replace")) for i in items)
    return _text(value).encode("utf-8", "replace")


def identity_of(parts: Sequence[Tuple[str, Any]]) -> str:
    """SHA-256 over ``(name, value)`` pairs, each half length-prefixed."""
    h = hashlib.sha256()
    for name, value in parts:
        h.update(_lp(_text(name).encode("utf-8", "replace")))
        h.update(_lp(_encode(value)))
    return h.hexdigest()


# ── inputs, read defensively ────────────────────────────────────────────────

class _Changes:
    """What the evidence block says changed on disk, and how much that answer
    is worth. `known` is False when no checkpoint and no snapshot could be
    taken at all — absence of a change list is not "nothing changed"."""

    def __init__(self, evidence: Any) -> None:
        d = evidence if isinstance(evidence, dict) else {}
        self.known = isinstance(evidence, dict)
        self.source = _text(d.get("source")) or ("" if self.known else "")
        self.added = _paths(d.get("added"))
        self.modified = _paths(d.get("modified"))
        self.deleted = _paths(d.get("deleted"))
        self.checkpoint = _text(d.get("checkpoint"))
        self.truncated = bool(d.get("truncated"))
        try:
            self.count = int(d.get("count"))
        except (TypeError, ValueError):
            self.count = len(self.added) + len(self.modified) + len(self.deleted)
        # Content-exact: the harness's shadow-repo diff. The mtime snapshot is
        # a fallback that skips generated folders and caps its walk, so its
        # silence about a path is not proof the path did not change.
        self.exact = self.source == "checkpoint"

    def all_paths(self) -> List[str]:
        return list(self.added) + list(self.modified) + list(self.deleted)

    def kind_of(self, path: str) -> Optional[str]:
        for kind, rows in (("added", self.added), ("modified", self.modified), ("deleted", self.deleted)):
            for q in rows:
                if _same_path(q, path):
                    return kind
        return None


class _Verification:
    """The dispatch verification block, read the way src/dispatch.py reads it:
    `ok is None` is *not verified*, never *passed*."""

    def __init__(self, verification: Any) -> None:
        d = verification if isinstance(verification, dict) else {}
        self.present = isinstance(verification, dict)
        self.mode = _text(d.get("mode"))
        self.ran = bool(d.get("ran"))
        self.ok = d.get("ok")
        self.inconclusive = bool(d.get("inconclusive"))
        self.pre_existing_only = bool(d.get("pre_existing_only"))
        self.command = _text(d.get("command"))
        self.summary = _text(d.get("summary"))
        self.failures = [_text(f) for f in (d.get("failures") or []) if _text(f)]

    @property
    def passed(self) -> bool:
        return self.ran and self.ok is True

    @property
    def failed(self) -> bool:
        return self.ran and self.ok is False and not self.inconclusive and not self.pre_existing_only

    @property
    def undecided(self) -> bool:
        """It ran and could not decide (or every failure predates the job)."""
        return self.ran and not self.passed and not self.failed


def _claims(claims: Any) -> Tuple[List[Tuple[str, str]], List[Dict[str, str]]]:
    """`claims` → [(kind, path)] plus the worker rows.

    Accepts what a caller has: a list of paths, a list of
    ``{"path", "kind"}``, or ``{"paths": [...], "workers": [{"name",
    "status", "outcome"}]}``. A claim without a kind can only ever be
    unaccounted for; one WITH a kind can also be contradicted.
    """
    rows: List[Tuple[str, str]] = []
    workers: List[Dict[str, str]] = []
    raw: Any = claims
    if isinstance(claims, dict):
        raw = claims.get("paths")
        if raw is None:
            raw = claims.get("claims")
        for w in claims.get("workers") or []:
            if isinstance(w, dict):
                workers.append({"name": _text(w.get("name")),
                                "status": _text(w.get("status")).strip().lower(),
                                "outcome": _text(w.get("outcome")).strip().lower()})
            else:
                workers.append({"name": _text(w), "status": "", "outcome": ""})
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        raw = []
    seen = set()
    for item in raw:
        kind, path = "", ""
        if isinstance(item, dict):
            path = _path(item.get("path") or item.get("file"))
            kind = _text(item.get("kind")).strip().lower()
        else:
            path = _path(item)
        if kind not in ("added", "modified", "deleted"):
            kind = ""
        if not path:
            continue
        key = (kind, path)
        if key in seen:
            continue
        seen.add(key)
        rows.append(key)
    return rows, workers


# ── the packet ──────────────────────────────────────────────────────────────

def _entry(kind: str, detail: str) -> Dict[str, str]:
    return {"kind": kind, "detail": detail}


def prove(evidence: Any, verification: Any, claims: Any, *, now: Optional[float] = None) -> Proof:
    """Reconcile what was OBSERVED with what was CLAIMED and say what that
    proves. See the module docstring for the four verdicts. Never raises."""
    try:
        return _prove(evidence, verification, claims, now=now)
    except Exception as e:  # noqa: BLE001 - a proof never raises into a settle path
        at = time.time() if now is None else now
        unc = [_entry(INTERNAL_ERROR, f"the proof could not be built: {type(e).__name__}: {e}"[:200])]
        return {
            "verdict": "unproved", "confidence": 0.0, "uncertainty": unc,
            "observations": [], "identity": identity_of([("schema", SCHEMA_VERSION), ("error", str(e)[:200])]),
            "schema_version": SCHEMA_VERSION, "at": at,
        }


def _prove(evidence: Any, verification: Any, claims: Any, *, now: Optional[float]) -> Proof:
    at = time.time() if now is None else now
    ch = _Changes(evidence)
    v = _Verification(verification)
    claim_rows, workers = _claims(claims)

    observations: List[Dict[str, str]] = []
    uncertainty: List[Dict[str, str]] = []

    def note(kind: str, detail: str) -> None:
        observations.append(_entry(kind, detail))

    def doubt(kind: str, detail: str) -> None:
        if not any(u["kind"] == kind for u in uncertainty):
            uncertainty.append(_entry(kind, detail))

    # ── what was observed ───────────────────────────────────────────────
    if not ch.known:
        doubt(NO_CHECKPOINT, "no checkpoint and no snapshot of the workspace could be taken — "
                             "nothing here can see what changed")
    else:
        note("changes", f"{ch.count} path(s) changed on disk"
                        + (f" ({ch.source})" if ch.source else "")
                        + (f" [checkpoint {ch.checkpoint}]" if ch.checkpoint else ""))
        if not ch.exact:
            doubt(MTIME_ONLY, "the change list comes from an mtime/size snapshot, not a content-exact "
                              "checkpoint diff: it skips generated folders and cannot see a reverted edit")
        if ch.truncated:
            doubt(TRUNCATED_CHANGES, "the change list is truncated — a path missing from it is not proof "
                                     "that it did not change")
    observed = ch.all_paths()
    if ch.known and not observed:
        doubt(NO_OBSERVABLE_CHANGE, "nothing changed on disk that this evidence can see")

    # ── the verification ────────────────────────────────────────────────
    if v.passed:
        note("verification", f"passed: {v.summary or v.command or 'the verification command'}")
        if v.pre_existing_only:
            doubt(PRE_EXISTING_FAILURES, "the failures that remain were already failing before the job")
    elif v.failed:
        note("verification", f"FAILED: {v.summary or v.command or 'the verification command'}")
        doubt(VERIFICATION_FAILED, (f"the verification failed: {v.summary or v.command or 'no summary'}"
                                    + (f" ({v.failures[0]})" if v.failures else ""))[:300])
    elif v.undecided:
        note("verification", f"inconclusive: {v.summary or v.command or 'no summary'}")
        if v.pre_existing_only:
            doubt(PRE_EXISTING_FAILURES, "every failure was already failing before the job — the job neither "
                                         "proved nor broke anything here")
        else:
            doubt(VERIFICATION_INCONCLUSIVE, f"the verification ran but could not decide: "
                                             f"{v.summary or 'no summary'}"[:300])
    else:
        note("verification", f"did not run: {v.summary or v.mode or 'no runner'}")
        doubt(NO_VERIFICATION_RUNNER, ("nothing ran that could prove the work: "
                                       + (v.summary or "no test runner and no verify command"))[:300])

    # ── the claims against the disk ─────────────────────────────────────
    unaccounted: List[str] = []
    contradicted: List[str] = []
    kind_mismatch: List[str] = []
    for kind, path in claim_rows:
        found = ch.kind_of(path) if ch.known else None
        if found is None:
            (contradicted if (ch.known and ch.exact and not ch.truncated) else unaccounted).append(path)
        elif kind and kind != found:
            kind_mismatch.append(f"{path}: claimed {kind}, observed {found}")
    if claim_rows:
        note("claims", f"{len(claim_rows)} path(s) claimed by the workers, "
                       f"{len(claim_rows) - len(unaccounted) - len(contradicted)} of them in the observed changes")
    if contradicted:
        doubt(CLAIM_NOT_ON_DISK, "the checkpoint diff is exact and does not contain: "
                                 + ", ".join(sorted(contradicted)[:8]))
    if unaccounted:
        doubt(CLAIMS_UNACCOUNTED, "claimed but not in the observed changes: "
                                  + ", ".join(sorted(unaccounted)[:8]))
    if kind_mismatch:
        doubt(CLAIM_KIND_MISMATCH, "the disk says otherwise: " + "; ".join(sorted(kind_mismatch)[:8]))

    # ── the workers themselves ──────────────────────────────────────────
    stopped, unfinished = [], []
    for w in workers:
        if w["outcome"] == "cancelled" or w["status"] in _CANCELLED:
            stopped.append(w)
        elif w["status"] and w["status"] not in _FINISHED_OK:
            unfinished.append(w)
    if workers:
        note("workers", f"{len(workers)} worker(s): "
                        + ", ".join(f"{w['name'] or '?'}={w['status'] or '?'}" for w in workers[:8]))
    if stopped:
        doubt(WORKER_CANCELLED, "stopped before finishing (not a failure, but the objective is not finished "
                                "either): " + ", ".join(sorted(w["name"] or "?" for w in stopped)[:8]))
    if unfinished:
        doubt(WORKER_UNFINISHED, "did not finish: "
              + ", ".join(sorted(f"{w['name'] or '?'} ({w['status']})" for w in unfinished)[:8]))

    # ── the verdict ─────────────────────────────────────────────────────
    claims_settled = not unaccounted and not contradicted and not kind_mismatch
    if v.failed or contradicted or kind_mismatch:
        verdict = "contradicted"
    elif v.passed and claims_settled and not stopped and not unfinished and (observed or not claim_rows):
        verdict = "proved"
    elif not v.ran and not observed:
        # Nothing ran that could prove it and nothing observable changed —
        # including the case where a worker CLAIMS work: a claim is not an
        # observation, and with no exact diff to contradict it (that branch is
        # above) the honest answer is that nothing here can show it.
        verdict = "unproved"
    else:
        verdict = "partial"

    total = sum(PENALTY.get(u["kind"], 0.1) for u in uncertainty)
    confidence = _clamp(min(CEILING.get(verdict, 0.5), 1.0 - total))
    # The invariant: a caller must always be able to read WHY the number is
    # not 1. A confidence below 1 with an empty uncertainty list would be a
    # silent claim, which is the whole thing this module exists to refuse.
    if confidence < 1.0 and not uncertainty:
        uncertainty.append(_entry("unspecified", f"the verdict is `{verdict}`, which cannot carry full confidence"))
    uncertainty.sort(key=lambda u: (-PENALTY.get(u["kind"], 0.1), u["kind"]))

    parts: List[Tuple[str, Any]] = [
        ("schema", SCHEMA_VERSION),
        ("evidence_known", ch.known),
        ("evidence_source", ch.source),
        ("evidence_checkpoint", ch.checkpoint),
        ("evidence_truncated", ch.truncated),
        ("added", ch.added),
        ("modified", ch.modified),
        ("deleted", ch.deleted),
        ("claims", [f"{k}:{p}" for k, p in claim_rows]),
        ("workers", [f"{w['name']}:{w['status']}:{w['outcome']}" for w in workers]),
        ("verification_present", v.present),
        ("verification_mode", v.mode),
        ("verification_ran", v.ran),
        ("verification_ok", v.ok if v.ok is None else bool(v.ok)),
        ("verification_inconclusive", v.inconclusive),
        ("verification_pre_existing_only", v.pre_existing_only),
        ("verification_command", v.command),
        ("verification_failures", v.failures),
    ]
    return {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "uncertainty": uncertainty,
        "observations": observations,
        "identity": identity_of(parts),
        "schema_version": SCHEMA_VERSION,
        "at": at,
    }


# ── what an agent Faustus did not write was, or was not, judged by ──────────

#: The sentence the blanket entry carries. Unchanged from the day the external
#: runner path shipped: a run nothing looked at still reads exactly the same.
EXTERNAL_UNGUARDED_DETAIL = ("an external agent ran its own shell; Faustus's command guard did not "
                             "see its commands")
EXTERNAL_UNGUARDED_PENALTY = PENALTY[EXTERNAL_UNGUARDED]


def _gate_ledgers(gates: Any) -> Dict[str, Dict[str, Any]]:
    """`gates` → {runner key: ledger}. Accepts a mapping or a list of ledgers."""
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(gates, dict):
        items: Iterable[Any] = gates.items()
    elif isinstance(gates, (list, tuple)):
        items = ((str((g or {}).get("runner") or ""), g) for g in gates if isinstance(g, dict))
    else:
        return out
    for key, led in items:
        name = _text(key).strip()
        if name and isinstance(led, dict):
            out[name] = led
    return out


def note_external_gate(proof: Any, runners: Any, *, gates: Any = None) -> Any:
    """Say, in the proof, what Faustus's own guard did and did not see.

    This is the entry src/prove.py cannot derive for itself: whether the agents
    that did the work were ones Faustus wrote. It has three answers and they
    are genuinely different, so it gives three:

    * **nothing gated it** — the blanket ``external_agent_unguarded``, exactly
      the entry and exactly the sentence the external-runner path has carried
      since it shipped. This is still the answer for every runner whose row
      says ``gate: "none"``;
    * **gated, and the gate judged everything** — the entry is DROPPED and an
      observation takes its place with the counts: how many calls were judged,
      how many refused. The confidence does not pay for a hole that is not
      there;
    * **gated, with something it did not judge** — a *narrower* entry,
      ``external_agent_tools_unjudged``, naming the tools. A gate that met a
      tool it had never heard of allowed it (better than breaking a foreign CLI
      the day it ships one) and the calls the CLI reported without a matching
      receipt are counted here too. Reporting partial coverage as full coverage
      would be precisely the dishonesty this module exists to prevent, so this
      case never rounds up to the second one.

    ``gates`` is ``{runner key: ledger}`` from src/agent_gate.py (or a list of
    ledgers carrying their own ``runner``). With no ledgers at all, every
    runner is ungated and the answer is byte-identical to what it was before
    the gate existed. Never raises: a proof that cannot be annotated is
    returned as it stands.
    """
    packet = proof
    keys = [_text(r).strip() for r in (runners or ()) if _text(r).strip()]
    if not isinstance(packet, dict) or not keys:
        return packet
    try:
        unc = list(packet.get("uncertainty") or [])
        if any(u.get("kind") in (EXTERNAL_UNGUARDED, EXTERNAL_TOOLS_UNJUDGED) for u in unc):
            return packet
        ledgers = _gate_ledgers(gates)

        gated: List[str] = []
        ungated: List[str] = []
        judged = denied = unjudged = unseen = 0
        tools: List[str] = []
        for key in keys:
            led = ledgers.get(key) or {}
            if not led.get("gated"):
                ungated.append(key)
                continue
            gated.append(key)
            judged += int(led.get("calls") or 0)
            denied += int(led.get("denied") or 0)
            unjudged += int(led.get("unjudged") or 0)
            unseen += int(led.get("unseen") or 0)
            for name in list(led.get("unjudged_tools") or []) + list(led.get("unseen_tools") or []):
                if _text(name) and _text(name) not in tools:
                    tools.append(_text(name))

        added: List[Dict[str, str]] = []
        if ungated:
            added.append(_entry(EXTERNAL_UNGUARDED,
                                EXTERNAL_UNGUARDED_DETAIL + " (" + ", ".join(ungated[:4]) + ")"))
        if gated and (unjudged or unseen):
            detail = (f"Faustus gated {', '.join(gated[:4])} and judged {judged} tool call(s), "
                      f"but {unjudged + unseen} of them were not judged")
            if tools:
                detail += " (" + ", ".join(sorted(tools)[:8]) + ")"
            detail += (": a tool name the gate does not recognise is allowed and recorded, and a "
                       "call the agent's own stream reports without a gate receipt is counted here "
                       "too")
            added.append(_entry(EXTERNAL_TOOLS_UNJUDGED, detail[:400]))

        obs = list(packet.get("observations") or [])
        if gated:
            obs.append(_entry("external_gate",
                              f"Faustus's guard judged {judged} tool call(s) from "
                              f"{', '.join(gated[:4])} before they ran and refused {denied}"))
        summary = {"gated": gated, "unguarded": ungated, "judged": judged, "denied": denied,
                   "unjudged": unjudged, "unseen": unseen}
        if tools:
            summary["unjudged_tools"] = sorted(tools)

        if not added:
            packet["observations"] = obs
            packet["external_gate"] = summary
            return packet

        unc.extend(added)
        unc.sort(key=lambda u: (-PENALTY.get(str(u.get("kind")), EXTERNAL_UNGUARDED_PENALTY),
                                str(u.get("kind"))))
        try:
            confidence = float(packet.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        cost = sum(PENALTY.get(str(u.get("kind")), EXTERNAL_UNGUARDED_PENALTY) for u in added)
        packet["uncertainty"] = unc
        packet["observations"] = obs
        packet["confidence"] = round(max(0.0, confidence - cost), 3)
        packet["external_gate"] = summary
        if ungated:
            # The key the dispatch payload and the runners page already read.
            packet["unguarded_runners"] = list(ungated)
    except Exception:  # noqa: BLE001 - a proof that cannot be annotated is returned as it is
        return packet
    return packet


def top_uncertainty(proof: Any) -> Optional[Dict[str, str]]:
    """The heaviest reason the confidence is not 1, or None when there is
    none (which only happens at confidence 1.0)."""
    try:
        rows = (proof or {}).get("uncertainty") or []
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


def line(proof: Any, *, detail_chars: int = 90) -> str:
    """One line for a verdict string: ``proof unproved (nothing ran that could
    prove the work: …)``. Empty when there is no proof."""
    try:
        p = proof or {}
        verdict = _text(p.get("verdict"))
        if not verdict:
            return ""
        top = top_uncertainty(p)
        out = f"proof {verdict} ({p.get('confidence')})"
        if top:
            detail = _text(top.get("detail"))
            if len(detail) > detail_chars:
                detail = detail[: detail_chars - 1].rstrip() + "…"
            out += f" — {top.get('kind')}: {detail}"
        return out
    except Exception:  # noqa: BLE001
        return ""


def enabled() -> bool:
    """Setting ``agent_dispatch_prove``. Off = a dispatched job's payload is
    exactly what it was before this module existed."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_dispatch_prove", True))
    except Exception:  # noqa: BLE001 - never raise into a settle path
        return True
