"""skill_governance.py — TOOL-06: a skill is evaluable context, not a
privileged instruction.

`services/memory/skills.py` already stores a skill as a `Skill` dataclass
(`services/memory/skill_format.py`) with `version`, `status` (draft/published),
`verification` (its own success evidence) and `requires_toolsets`. What is
missing, and what this module adds without a second store or a competing
schema, is the governance layer the plan asks for on top of that shape:

1. **A skill never becomes a rule nobody reviewed.** `validate_promotion`
   refuses to move a skill demonstrated in Enséñame (Teach Mode) — or any
   other unreviewed origin — into a status other projects pick up, unless a
   caller can show it was reviewed. This module does not decide WHO reviews;
   it is the one gate every promotion path must pass through, so "reviewed"
   cannot be true by a route simply forgetting to check.
2. **An obsolete skill fails before it mutates anything**, and hands back the
   most recent version that was NOT obsolete instead of leaving a caller with
   nothing (`gate_mutating_use`). "Obsolete" here is a closed vocabulary
   (`OBSOLETE_STATUSES`) layered on top of `draft`/`published` — additive,
   because a skill in either of those existing statuses is untouched by this
   gate (`is_obsolete` is False for both), exactly today's behaviour.
3. **A skill's own words never grant it more access.** `as_context_message`
   is how a skill is meant to reach a prompt: `role: "context"`, its
   provenance as structured metadata alongside the text, and
   `privilege_flags` — a deterministic scan for the small set of phrases that
   would read as the skill instructing the agent to act with more privilege
   than the user granted (SEC-01/TOOL-04 stay the actual gate; this is the
   signal a caller checks before trusting an unreviewed skill's content at
   all). A skill is never the thing that decides its own privilege.
4. **Evaluable means the fields the eval suite needs are actually there.**
   `evaluation_manifest` names what is missing (inputs, success evidence,
   version) instead of a caller discovering it mid-run — it does not run the
   suite itself (`routes/skills_routes.py` already owns that).

Every function here takes and returns plain mappings/lists — the same shape
`Skill.to_dict()`-like readers already produce elsewhere in the repo — so
nothing in `services/memory/skills.py` needs to change for this module to be
useful, and nothing here can drift into a parallel skills store.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: Closed, additive vocabulary layered over the existing draft/published
#: statuses (services/memory/skill_format.py). A skill in either of those is
#: NOT obsolete by this module's own reading — nothing about today's
#: draft/published behaviour changes.
OBSOLETE_STATUSES = frozenset({"obsolete", "deprecated", "superseded"})

#: Actions that actually change something (apply a procedure's steps, run a
#: tool the skill names, publish/promote it). Read-only uses — retrieval,
#: display, simulation/dry-run — are never blocked by this gate: an obsolete
#: skill may still be inspected, just never acted on.
MUTATING_ACTIONS = frozenset({"apply", "run", "execute", "invoke", "promote", "publish"})

#: Origins that must pass an explicit review before a skill demonstrated
#: there is trusted with a status other projects can pick up. "learned"
#: (services/memory/skill_format.Skill's own default `source`) is
#: deliberately absent: only the origins that name Teach Mode specifically
#: are gated here, so an ordinary authored/learned skill's promotion is
#: unaffected by this module's existence.
REVIEW_REQUIRED_ORIGINS = frozenset({"teach_mode", "demonstration", "teacher-escalation"})

#: Statuses a promotion may not reach without review, for a gated origin.
_PUBLISHED_LIKE_STATUSES = frozenset({"published", "active", "global"})

__all__ = [
    "OBSOLETE_STATUSES", "MUTATING_ACTIONS", "REVIEW_REQUIRED_ORIGINS",
    "SkillVersionEntry", "is_obsolete", "gate_mutating_use",
    "privilege_escalation_flags", "as_context_message", "validate_promotion",
    "record_version", "evaluation_manifest", "lint_skill",
]


def is_obsolete(skill: Mapping[str, Any]) -> bool:
    return str((skill or {}).get("status") or "").strip().lower() in OBSOLETE_STATUSES


def _version_sort_key(version: Any) -> tuple:
    """Best-effort ordering for `Skill.version` (a free-text string like
    "1.2.0" per skill_format.py's default). Falls back to the raw string so a
    non-numeric version still sorts deterministically instead of raising."""
    parts = re.findall(r"\d+", str(version or "0"))
    return tuple(int(p) for p in parts) if parts else (0,)


def _latest_non_obsolete(history: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = [dict(entry) for entry in (history or []) if not is_obsolete(entry)]
    if not candidates:
        return None
    return max(candidates, key=lambda e: _version_sort_key(e.get("version")))


def gate_mutating_use(skill: Mapping[str, Any], *, action: str,
                       history: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    """TOOL-06 acceptance: an obsolete skill fails BEFORE it mutates
    anything, and the refusal carries the previous (non-obsolete) version
    when `history` has one, so the caller is never left with just a no.

    `action` outside `MUTATING_ACTIONS` (a read, a preview, a simulation) is
    always allowed regardless of status — this gate only ever stands between
    an obsolete skill and an effect.
    """
    act = str(action or "").strip().lower()
    if act not in MUTATING_ACTIONS:
        return {"allowed": True, "reason": "", "fallback_version": None}
    if not is_obsolete(skill):
        return {"allowed": True, "reason": "", "fallback_version": None}
    status = (skill or {}).get("status")
    return {
        "allowed": False,
        "reason": f"skill status is {status!r} — refused before {act!r} with an obsolete skill",
        "fallback_version": _latest_non_obsolete(history),
    }


# ── content never elevates privilege ────────────────────────────────────────

#: Deterministic, no model call. Each pattern names a phrase whose only
#: plausible purpose in skill text is telling the agent to act with more
#: privilege than the user granted through the actual permission system
#: (src/security_policy.py, src/subagent_permissions.py). This is a signal
#: for a caller to act on, not itself an enforcement point — it can flag a
#: skill; it cannot, and does not try to, block a tool call.
_ESCALATION_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|earlier)\s+instructions",
    r"you\s+(?:are|'re)\s+now\s+(?:in\s+)?(?:developer|admin|god|unrestricted)\s+mode",
    r"disable\s+(?:all\s+)?(?:safety|guard|approval|confirmation)s?",
    r"bypass\s+(?:the\s+)?(?:approval|sandbox|permission|confirmation)s?",
    r"grant\s+(?:yourself|this\s+skill|the\s+agent)\s+(?:more|full|admin|root)\s+(?:access|permission|privilege)",
    r"run\s+(?:without|skipping)\s+(?:confirmation|approval)",
    r"act\s+as\s+(?:the\s+)?system",
    r"do\s+not\s+ask\s+(?:for\s+)?(?:approval|permission|confirmation)",
    r"elevate\s+(?:your\s+|the\s+)?(?:permission|privilege)",
))


def privilege_escalation_flags(markdown: str) -> List[str]:
    """The escalation phrases found in `markdown`, or `[]` for clean text.
    Never raises; an empty/None input is simply no flags."""
    text = markdown or ""
    return [p.pattern for p in _ESCALATION_PATTERNS if p.search(text)]


def as_context_message(skill: Mapping[str, Any], markdown: str) -> Dict[str, Any]:
    """A skill rendered the way it is meant to reach a prompt: `role`
    literally `"context"` (never `"system"` or `"instruction"`), its
    provenance as a structured sibling field, and any privilege-escalation
    flags travelling WITH the content instead of only inside it — so a
    caller can refuse an unreviewed, flagged skill without re-scanning text
    that already made its own claims about itself."""
    return {
        "role": "context",
        "provenance": {
            "kind": "skill",
            "name": (skill or {}).get("name") or (skill or {}).get("id") or "",
            "version": (skill or {}).get("version") or "",
            "status": (skill or {}).get("status") or "",
            "source": (skill or {}).get("source") or "",
            "reviewed": bool((skill or {}).get("reviewed")),
        },
        "privilege_flags": privilege_escalation_flags(markdown),
        "content": markdown or "",
    }


# ── promotion from Teach Mode requires review ───────────────────────────────

def validate_promotion(skill: Mapping[str, Any], *, target_status: str, reviewed: bool) -> Dict[str, Any]:
    """TOOL-06: promotion from Enséñame (or another demonstrated origin) to a
    status other projects/users can pick up requires `reviewed=True` from the
    caller — never inferred, never defaulted to true. A skill authored
    directly (`source` outside `REVIEW_REQUIRED_ORIGINS`) is unaffected: its
    promotion is exactly as permissive as it was before this function
    existed.
    """
    origin = str((skill or {}).get("source") or (skill or {}).get("origin") or "").strip().lower()
    target = str(target_status or "").strip().lower()
    if target in _PUBLISHED_LIKE_STATUSES and origin in REVIEW_REQUIRED_ORIGINS and not reviewed:
        return {
            "ok": False,
            "reason": f"promotion from {origin!r} to {target!r} requires review before it becomes a rule others pick up",
        }
    return {"ok": True, "reason": ""}


# ── version history (pure — the caller owns persistence) ───────────────────

@dataclass(frozen=True)
class SkillVersionEntry:
    name: str
    version: str
    content_hash: str
    status: str = "draft"
    origin: str = "manual"
    reviewed: bool = False
    supersedes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "content_hash": self.content_hash,
            "status": self.status, "origin": self.origin, "reviewed": self.reviewed,
            "supersedes": self.supersedes,
        }


def record_version(history: Sequence[Mapping[str, Any]], *, name: str, content: str,
                    status: str = "draft", origin: str = "manual",
                    reviewed: bool = False) -> Dict[str, Any]:
    """Append one version entry, derived from `content`'s own hash (so two
    identical publishes of the same text produce the same `content_hash`
    rather than a new version number nobody can tell apart) and pointing
    `supersedes` at whichever prior entry in `history` shares this `name` and
    was not already obsolete. Returns the NEW entry only — `history` is
    never mutated; the caller decides how (and whether) to persist
    `[*history, new_entry]`.
    """
    content_hash = hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]
    same_name = [dict(e) for e in (history or []) if str(e.get("name") or "") == name]
    prior = _latest_non_obsolete(same_name)
    next_n = 1 + max(_version_sort_key(e.get("version"))[0] for e in same_name) if same_name else 1
    entry = SkillVersionEntry(
        name=name, version=f"{next_n}.0.0", content_hash=content_hash,
        status=status, origin=origin, reviewed=bool(reviewed),
        supersedes=str(prior.get("content_hash") or "") if prior else "",
    )
    return entry.to_dict()


# ── evaluable: has an eval suite got what it needs ──────────────────────────

def evaluation_manifest(skill: Mapping[str, Any]) -> Dict[str, Any]:
    """What TOOL-06's backend text calls entradas, evidencias de éxito,
    versiones, dependencias — read off the fields `services/memory/
    skill_format.Skill` already carries (`when_to_use` as the input
    description, `verification` as success evidence, `version`,
    `requires_toolsets` as dependencies), plus which of them are missing.
    Does not run anything: `routes/skills_routes.py` already owns the actual
    eval suite (`test_skill`/audit machinery); this only says whether a
    skill is ready to be handed to it.
    """
    skill = skill or {}
    missing: List[str] = []
    if not str(skill.get("when_to_use") or "").strip():
        missing.append("when_to_use (inputs)")
    if not (skill.get("verification") or []):
        missing.append("verification (success evidence)")
    if not str(skill.get("version") or "").strip():
        missing.append("version")
    return {
        "name": skill.get("name") or "",
        "version": skill.get("version") or "",
        "dependencies": list(skill.get("requires_toolsets") or []),
        "success_evidence": list(skill.get("verification") or []),
        "missing": missing,
        "evaluable": not missing,
    }


# ── writing-for-agents linter (deterministic, no model) ─────────────────────

#: Frontmatter keys a SKILL.md may carry; `manual_only` is the point-3 flag:
#: a skill that sets it true is kept out of the automatic selector index and
#: is only ever loaded by an explicit `manage_skills action=view`.
_FRONTMATTER_KEYS = ("name", "description", "version", "category", "tags",
                     "status", "confidence", "source", "owner", "created",
                     "when_to_use", "manual_only")

#: Generic names that say nothing about when to fire. A skill named one of
#: these (after slug-normalisation) is rejected at learn time — the 22:39
#: `general/skill` entry is the case this closes.
_GENERIC_NAMES = frozenset({"skill", "skills", "test", "temp", "new", "draft",
                            "misc", "stuff", "notes", "helper", "util", "utils"})

#: UTF-8 read as cp1252/latin-1 leaves these artefacts. Their presence in a
#: learned skill means the import path decoded with the wrong codec.
_MOJIBAKE_PATTERNS = ("â€", "Ã©", "Ã­", "Ã³", "Ãº", "Ã±", "Â¿", "Â¡", "Ã‘", "Ã'")

#: Negation openers that, without a positive action in the same sentence, are
#: weak guidance for a small model (writing-for-agents check 3).
_NEGATION_RE = re.compile(
    r"(?<!\w)(?:no |never |don't |do not |nunca |no uses |no hagas )", re.I)


def _split_frontmatter(markdown: str) -> Dict[str, str]:
    """Best-effort `key: value` read of a SKILL.md frontmatter block. Tolerant
    of missing/unclosed blocks: whatever parses, parses; the linter reports
    what it can see rather than raising."""
    out: Dict[str, str] = {}
    lines = (markdown or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            out[m.group(1).lower()] = m.group(2).strip().strip('"').strip("'")
    return out


def lint_skill(markdown: str) -> List[Dict[str, str]]:
    """writing-for-agents checks over one SKILL.md, as plain findings.

    Each finding is `{"check", "severity", "message"}` with severity in
    {blocking, major, minor}:
    - blocking: no actionable steps, or no verifiable completion criterion;
    - major: description that doesn't say when to fire, prose mention of
      another skill where a literal call is needed;
    - minor: bare negations without the positive action beside them.

    Never raises; empty/None input yields the blocking findings it deserves.
    The linter marks — the caller (learn/promote path) decides what to do.
    """
    text = markdown or ""
    findings: List[Dict[str, str]] = []
    fm = _split_frontmatter(text)
    body = text.split("---", 2)[2] if text.count("---") >= 2 else text

    # 1. description as a trigger pointer: it must say WHEN to use it.
    desc = fm.get("description", "")
    if desc and not re.search(
            r"(?i)\b(when|use when|cuando|para|usar cuando|si |antes de|trigger)\b", desc):
        findings.append({
            "check": "description-trigger", "severity": "major",
            "message": "description says what the skill is, not when to use it; "
                       "rewrite it as a trigger ('Use when…' + the words the user would say)",
        })

    # 2. no-op test: the body must contain actionable steps.
    has_steps = bool(re.search(r"(?m)^\s*(?:\d+\.|[-*+]\s)", body)) or "procedure" in fm
    if not has_steps:
        findings.append({
            "check": "no-op", "severity": "blocking",
            "message": "no numbered/bulleted steps in the body: loading this skill "
                       "would change nothing an agent does",
        })

    # 3. negations without a positive action nearby.
    for line in body.splitlines():
        if _NEGATION_RE.search(line) and not re.search(
                r"(?i)\b(use|do|run|call|prefer|usa|haz|ejecuta|llama|prefiere)\b", line):
            findings.append({
                "check": "negation", "severity": "minor",
                "message": f"negation without the positive action beside it: {line.strip()[:80]!r}",
            })
            break  # one representative finding is enough

    # 4. verifiable completion criterion.
    if not re.search(r"(?i)\b(verify|check|expect|pass|exit code|verifica|verificar|comprueba|comprobar|compara|comparar|espera)\b", body):
        findings.append({
            "check": "done-criterion", "severity": "blocking",
            "message": "no verifiable completion criterion (command to run, expected output); "
                       "'it's done' without a check is decoration",
        })

    # 5. prose mention of another skill where a literal call is needed.
    for m in re.finditer(r"(?i)\b(?:use|see|ver|usa)\s+(?:the\s+)?skill\s+[`\"']?([\w-]+)", body):
        findings.append({
            "check": "literal-call", "severity": "major",
            "message": f"mentions skill {m.group(1)!r} in prose; if the reader must RUN it, "
                       "call it literally (manage_skills action=view name=<slug>) at that step",
        })

    # hygiene: mojibake and generic names (the 22:39 general/skill case).
    if any(p in text for p in _MOJIBAKE_PATTERNS):
        findings.append({
            "check": "mojibake", "severity": "major",
            "message": "text contains mojibake (UTF-8 decoded with the wrong codec); "
                       "re-import with the correct encoding",
        })
    name = (fm.get("name") or "").strip().lower().replace("_", "-")
    if name in _GENERIC_NAMES:
        findings.append({
            "check": "generic-name", "severity": "major",
            "message": f"skill name {name!r} is generic and says nothing about when to fire; "
                       "rename to what the skill does",
        })

    return findings
