"""
writing_style.py — WRITE-03: style editable and non-invasive.

The existing "Aprender un estilo" flow (`/api/presets/style/derive` and
`/compare`, `studio/src/screens/studio/StyleLab.tsx`) already extracts style
rules from examples with a model and saves the result as a flat preset
system-prompt (rule 4: reused here, not re-derived — `from_learned_preset`
below wraps that flow's OUTPUT, it never talks to a model itself). What
that flow has no place for is SCOPE: a style learned from a novel
manuscript has no business steering a professional email, and "turn it
off" has to actually remove it from the next context build, not merely
hide a UI toggle.

A `StyleGuide` wraps one or more versioned rule sets (`StyleGuideVersion` —
rules with examples, prohibitions and a per-rule intensity floor) under a
declared `scope` (tags such as ``project:my-novel`` or ``email``) and an
`enabled` flag. `compile_context` is the one function a caller (a
system-prompt builder, outside this batch) needs: it returns `None`
whenever the guide must contribute NOTHING to context — disabled, or the
requested scope does not match — which is what makes "desactivar retira
realmente del contexto" and "no contamina" checkable properties instead of
a description of intent.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.contracts.base import now_iso


@dataclass
class StyleRule:
    text: str
    examples: List[str] = field(default_factory=list)
    prohibitions: List[str] = field(default_factory=list)
    #: Rules below this floor drop out when the guide's `intensity` is
    #: turned down — "ajustar intensidad" made literal rather than a
    #: single on/off per guide. 0 = always in force at any intensity > 0.
    min_intensity: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "examples": list(self.examples),
                "prohibitions": list(self.prohibitions),
                "min_intensity": self.min_intensity}


@dataclass
class StyleGuideVersion:
    version: int
    rules: List[StyleRule]
    created_at: str
    #: Provenance ("ejemplos, prohibiciones y ambito" needs a *where this
    #: came from*, not just the rules themselves) — "derived" (through
    #: `from_learned_preset`, i.e. the existing Learn-a-style flow) or
    #: "manual" (typed/edited directly).
    source: str = "manual"

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "rules": [r.to_dict() for r in self.rules],
                "created_at": self.created_at, "source": self.source}


@dataclass
class StyleGuide:
    id: str
    name: str
    scope: List[str]
    enabled: bool = True
    intensity: float = 1.0
    versions: List[StyleGuideVersion] = field(default_factory=list)

    def current(self) -> Optional[StyleGuideVersion]:
        return self.versions[-1] if self.versions else None

    def to_dict(self) -> Dict[str, Any]:
        current = self.current()
        return {"id": self.id, "name": self.name, "scope": list(self.scope),
                "enabled": self.enabled, "intensity": self.intensity,
                "versions": [v.to_dict() for v in self.versions],
                "current_version": current.version if current else None}


def new_guide(name: str, *, scope: List[str]) -> StyleGuide:
    return StyleGuide(id=f"style_{uuid.uuid4().hex[:12]}", name=name, scope=list(scope))


def add_version(guide: StyleGuide, rules: List[StyleRule], *,
                source: str = "manual") -> StyleGuideVersion:
    """A NEW version, appended — the previous one stays in `guide.versions`
    (versioning, not overwriting), so a caller comparing "what changed
    between v2 and v3" always has both to read."""
    version = StyleGuideVersion(version=len(guide.versions) + 1, rules=list(rules),
                                created_at=now_iso(), source=source)
    guide.versions.append(version)
    return version


def _scope_matches(guide_scope: List[str], target_scope: str) -> bool:
    return "*" in guide_scope or target_scope in guide_scope


def effective_rules(guide: StyleGuide) -> List[StyleRule]:
    """Rules actually in force at the guide's CURRENT intensity — "ver
    reglas efectivas". A rule whose `min_intensity` exceeds the dial is not
    in force, though it stays stored: turning intensity back up brings it
    back, nothing here ever deletes a rule."""
    version = guide.current()
    if not guide.enabled or version is None:
        return []
    return [r for r in version.rules if guide.intensity >= r.min_intensity]


def compile_context(guide: StyleGuide, *, target_scope: str) -> Optional[str]:
    """The text a system-prompt builder may append for `target_scope`, or
    `None` when this guide must contribute nothing at all.

    `None` on `enabled=False` (a disabled guide is out of the NEXT context
    build, not merely greyed out in a UI) and on a scope mismatch (a
    novel's prohibitions never reach an email) are the two literal checks
    the acceptance line asks for.
    """
    if not guide.enabled:
        return None
    if not _scope_matches(guide.scope, target_scope):
        return None
    rules = effective_rules(guide)
    if not rules:
        return None
    lines = [f"Style guide: {guide.name} (intensity {guide.intensity:.2f})"]
    for rule in rules:
        lines.append(f"- {rule.text}")
        for prohibition in rule.prohibitions:
            lines.append(f"  never: {prohibition}")
        for example in rule.examples[:2]:
            lines.append(f"  example: {example}")
    return "\n".join(lines)


def from_learned_preset(name: str, *, rules_text: str, scope: List[str]) -> StyleGuide:
    """Adapt the EXISTING "Aprender un estilo" output (`rules` — one flat
    freeform string, `/api/presets/style/derive`'s own response shape) into
    a scoped, versioned guide, one rule per non-empty line. Nothing here
    talks to a model or re-derives anything; it only wraps what that flow
    already produced with the scope/version fields it has no column for.
    """
    lines = [ln.strip("-•* \t") for ln in (rules_text or "").splitlines() if ln.strip()]
    rules = [StyleRule(text=ln) for ln in lines] or [StyleRule(text=(rules_text or "").strip())]
    guide = new_guide(name, scope=scope)
    add_version(guide, rules, source="derived")
    return guide
