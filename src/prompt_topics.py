"""src/prompt_topics.py — a small, deterministic topic label for a prompt.

Used to split comparison votes into per-topic ratings. It is a keyword scorer
(Spanish and English), not a model: it costs nothing, gives the same answer for
the same prompt every time (so a rating can be recomputed from stored prompts
at any moment), and says "general" instead of guessing when nothing fits.

The conversation-level topic helper (`src/topic_analyzer.py`) was looked at
first and not reused: it labels whole chats with broad life-area topics in
English and matches substrings (a two-letter keyword fires inside unrelated
words), which is the wrong grain for "which model answers THIS kind of prompt
best".
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

GENERAL = "general"

# topic -> (phrases matched as whole words/phrases, regexes)
_TOPICS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "code": (
        ("python", "javascript", "typescript", "java", "rust", "golang", "sql", "html", "css", "react",
         "function", "función", "funcion", "bug", "error", "stack trace", "traceback", "compile", "compilar",
         "refactor", "refactoriza", "código", "codigo", "code", "script", "api", "endpoint", "regex", "algoritmo",
         "algorithm", "unit test", "test unitario", "debug", "depura", "clase", "class", "import", "git"),
        (r"```", r"\bdef \w+\(", r"\bconsole\.log\b", r"\bSELECT\b.+\bFROM\b"),
    ),
    "math": (
        ("calcula", "calculate", "ecuación", "ecuacion", "equation", "integral", "derivada", "derivative",
         "probabilidad", "probability", "demuestra", "prove", "teorema", "theorem", "matriz", "matrix",
         "álgebra", "algebra", "geometría", "geometry", "estadística", "statistics", "suma", "resuelve", "solve"),
        (r"\d+\s*[\+\-\*/\^]\s*\d+", r"\\frac|\\int|\\sum"),
    ),
    "translation": (
        ("traduce", "traducción", "traduccion", "translate", "translation", "en inglés", "en ingles",
         "al español", "into english", "into spanish", "in french", "en francés", "en frances"),
        (),
    ),
    "summarization": (
        ("resume", "resumen", "summarize", "summary", "tl;dr", "tldr", "sintetiza", "condense", "abstract",
         "puntos clave", "key points", "extrae", "extract"),
        (),
    ),
    "writing": (
        ("escribe", "redacta", "write", "draft", "email", "correo", "carta", "letter", "ensayo", "essay",
         "poema", "poem", "cuento", "story", "historia", "artículo", "article", "blog", "guion", "script for",
         "reescribe", "rewrite", "corrige", "proofread", "tono", "tone", "mejora este texto", "slogan"),
        (),
    ),
    "reasoning": (
        ("razona", "reason", "paso a paso", "step by step", "lógica", "logica", "logic", "puzzle", "acertijo",
         "riddle", "deduce", "infer", "por qué", "why does", "demuestra que", "paradoja", "paradox",
         "qué pasaría", "what would happen", "compara", "compare", "pros y contras", "pros and cons"),
        (),
    ),
    "knowledge": (
        ("quién", "quien", "who is", "who was", "cuándo", "when did", "dónde", "where is", "capital de",
         "capital of", "define", "qué es", "que es", "what is", "what are", "explica", "explain", "historia de",
         "history of", "diferencia entre", "difference between"),
        (),
    ),
}

_COMPILED: Dict[str, Tuple[List["re.Pattern[str]"], List["re.Pattern[str]"]]] = {}


def _compile() -> None:
    if _COMPILED:
        return
    for topic, (phrases, regexes) in _TOPICS.items():
        word_res = []
        for p in phrases:
            esc = re.escape(p)
            # whole-word match for phrases made of word characters at the edges
            left = r"(?<!\w)" if re.match(r"\w", p[0]) else ""
            right = r"(?!\w)" if re.match(r"\w", p[-1]) else ""
            word_res.append(re.compile(left + esc + right, re.IGNORECASE))
        _COMPILED[topic] = (word_res, [re.compile(r, re.IGNORECASE | re.DOTALL) for r in regexes])


TOPICS = tuple(_TOPICS) + (GENERAL,)


def topic_scores(prompt: str) -> Dict[str, int]:
    _compile()
    text = (prompt or "")[:4000]
    scores: Dict[str, int] = {}
    for topic, (words, regexes) in _COMPILED.items():
        n = sum(1 for rx in words if rx.search(text)) + 2 * sum(1 for rx in regexes if rx.search(text))
        if n:
            scores[topic] = n
    return scores


def classify(prompt: str) -> str:
    """The best-scoring topic, or ``general`` when nothing matched. Ties go to
    the topic listed first in `TOPICS` (code before math before ...), which
    keeps the answer stable."""
    scores = topic_scores(prompt)
    if not scores:
        return GENERAL
    order = {t: i for i, t in enumerate(TOPICS)}
    return sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0]]))[0][0]


__all__ = ["TOPICS", "GENERAL", "classify", "topic_scores"]
