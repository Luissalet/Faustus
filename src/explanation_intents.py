"""Detect explanation-only user turns for the no-workspace-action harness guard.

A preface ("Prueba de conexión…") or an explicit "don't use tools / don't take
actions" clause used to leave `_explanation_request` false when the explain cue
was not at column 0, so a correct prose answer was nudged into a second round.

Classic openings such as "¿Cómo funciona esto?" and "Dónde está server.py"
must keep working: the previous regex accepted leading ¿ / quotes and the
Spanish "dónde está" cue.
"""

from __future__ import annotations

import re

# Leading junk the old ^-anchored regex stripped: whitespace, inverted ?, quotes.
_LEAD = r"[\s¿\"'`]*"

# Explanation cues may open the turn or follow a short preface / clause boundary.
_EXPLANATION_CUE_RE = re.compile(
    r"(?:^" + _LEAD + r"|(?:[\n.!?：:;]\s*)" + _LEAD + r")"
    r"(?:por favor[,\s]+|please[,\s]+)?(?:me\s+)?"
    r"(?:puedes\s+|podr[íi]as\s+|can\s+you\s+|could\s+you\s+|please\s+)?"
    r"(?:expl[íi]ca(?:me)?|describe(?:me)?|descr[íi]beme|res[úu]me(?:me)?|res[úu]meme|"
    r"qu[ée]\s+hace|para\s+qu[ée]\s+sirve|"
    r"c[óo]mo\s+(?:funciona|comprobar\w*|distingu\w*|verific\w*|probar\w*)|"
    r"d[óo]nde\s+est[áa]|"
    r"explain|describe|summari[sz]e|walk\s+me\s+through|"
    r"what\s+does|what\s+is|what'?s|how\s+(?:does|would|can|do|to)\b|where\s+is)\b",
    re.IGNORECASE,
)

# Explicit refusal of tools/actions. When present with an explanation cue, a
# soft verb in the preface ("Prueba de conexión") must not force a coding nudge.
_ACTION_NEGATION_RE = re.compile(
    r"\b(?:"
    r"sin\s+(?:ejecutar\s+)?acciones?"
    r"(?:\s+ni\s+(?:usar\s+)?herramientas?)?|"
    r"sin\s+usar\s+herramientas?|"
    r"no\s+(?:ejecutes|hagas|uses|llames)\s+"
    r"(?:acciones?|herramientas?|tools?)|"
    r"without\s+(?:using\s+)?(?:any\s+)?(?:tools?|actions?)|"
    r"do\s+not\s+(?:run|use|call|execute)\s+(?:any\s+)?(?:tools?|actions?)|"
    r"don'?t\s+(?:run|use|call|execute)\s+(?:any\s+)?(?:tools?|actions?)|"
    r"no\s+tool\s+calls?"
    r")\b",
    re.IGNORECASE,
)

# Real work requests that must stay coding even if someone also said "explain".
_STRONG_CODE_WORK_RE = re.compile(
    r"\b(?:"
    r"impl[eé]m[eé]nt\w*|arregla\w*|corrige\w*|refactor\w*|"
    r"a[ñn]ade\w*|agrega\w*|modifica\w*|elimina\w*|borra\w*|"
    r"fix\s+the|implement\w*|add\s+(?:a\s+)?(?:button|route|file|test)|"
    r"crea\s+(?:un\s+)?(?:archivo|fichero|test|endpoint)|write\s+code|"
    r"aplica\s+(?:el\s+)?(?:parche|cambio|fix)"
    r")\b",
    re.IGNORECASE,
)


def is_explanation_request(text: str, *, coding_action_re: re.Pattern[str] | None = None) -> bool:
    """True when the user asked to be told something, not to change the workspace.

    ``coding_action_re`` is the caller's broad action heuristic (e.g. agent_loop's
    ``_WORKSPACE_CODE_ACTION_RE``). With an explicit action/tool negation it is
    ignored so a preface verb cannot force a second harness round.
    """
    text = str(text or "").strip()
    if not text or not _EXPLANATION_CUE_RE.search(text):
        return False
    if _STRONG_CODE_WORK_RE.search(text):
        return False
    if _ACTION_NEGATION_RE.search(text):
        return True
    if coding_action_re is not None and coding_action_re.search(text):
        return False
    return True
