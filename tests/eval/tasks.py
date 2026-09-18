"""tests/eval/tasks.py — EVAL-01's six representative tasks: bug fix,
feature, refactor, investigation over mocked (local, standing-in) sources, a
document, and a tabular-data answer. Each is a `Task`: how to build the
workspace, the exact scripted model answers `harness.EvalApp.script()` will
hand back in order, and how to check the RESULT — never the model's claim
about it.

Every task's `verify` returns a plain dict with at least `ok` (bool) and
`detail` (str); the three code-editing tasks also carry `verification`, the
raw `src.verification.run_verifier` result — EVAL-01's "resultado verificado
por src/verification.py". The other three (investigation/document/tabular)
have no test suite to run: DOCUMENT and TABULAR are checked against the
fixture's own known-correct content (deterministic, not a verifier module —
see the lot report's Limitaciones), and INVESTIGATION is checked through
`src.verification.verify_citation` (VER-05) instead, which is exactly the
right tool for "did the produced text earn its claim from the source it
read" and the only one of the three that has a `src/verification.py`
function of its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List


@dataclass(frozen=True)
class Task:
    name: str
    category: str
    message: str
    script: List[str]
    setup: Callable[[Path], None]
    verify: Callable[[Path, Any], Dict[str, Any]]


# ---------------------------------------------------------------------------
# 1. Bug fix
# ---------------------------------------------------------------------------

def _bug_fix_setup(ws: Path) -> None:
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (ws / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")


def _bug_fix_verify(ws: Path, result: Any) -> Dict[str, Any]:
    from src import verification
    v = verification.run_verifier("tests", str(ws), changed=["calc.py"])
    ok = bool(v.get("ran")) and not v.get("inconclusive") and not v.get("new_failures") and v.get("ok") is True
    return {"ok": ok, "detail": v.get("summary"), "verification": v}


BUG_FIX = Task(
    name="bug_fix", category="bug_fix",
    message="calc.add resta en vez de sumar. Corrígelo y confirma que los tests pasan.",
    script=[
        '```read_file\n{"path": "calc.py"}\n```',
        '```edit_file\n{"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}\n```',
        "He corregido calc.add: ahora suma en lugar de restar. Los tests deberían pasar.",
    ],
    setup=_bug_fix_setup, verify=_bug_fix_verify,
)


# ---------------------------------------------------------------------------
# 2. Feature
# ---------------------------------------------------------------------------

def _feature_setup(ws: Path) -> None:
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "tests" / "test_calc.py").write_text(
        "from calc import add, multiply\n\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_multiply():\n    assert multiply(2, 3) == 6\n",
        encoding="utf-8")
    (ws / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")


def _feature_verify(ws: Path, result: Any) -> Dict[str, Any]:
    from src import verification
    v = verification.run_verifier("tests", str(ws), changed=["calc.py"])
    ok = bool(v.get("ran")) and not v.get("inconclusive") and not v.get("new_failures") and v.get("ok") is True
    return {"ok": ok, "detail": v.get("summary"), "verification": v}


FEATURE = Task(
    name="feature", category="feature",
    message="Añade multiply(a, b) a calc.py; el test ya existe y hoy falla porque la función no existe.",
    script=[
        '```read_file\n{"path": "calc.py"}\n```',
        '```write_file\n{"path": "calc.py", "content": "def add(a, b):\\n    return a + b\\n\\n\\ndef multiply(a, b):\\n    return a * b\\n"}\n```',
        "He añadido multiply(a, b) a calc.py conservando add().",
    ],
    setup=_feature_setup, verify=_feature_verify,
)


# ---------------------------------------------------------------------------
# 3. Refactor (behaviour preserved, duplication removed)
# ---------------------------------------------------------------------------

def _refactor_setup(ws: Path) -> None:
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    (ws / "shapes.py").write_text(
        "def area_rectangle(w, h):\n    return w * h\n\n\n"
        "def area_square(s):\n    return s * s\n",
        encoding="utf-8")
    (ws / "tests" / "test_shapes.py").write_text(
        "from shapes import area_rectangle, area_square\n\n\n"
        "def test_area_rectangle():\n    assert area_rectangle(3, 4) == 12\n\n\n"
        "def test_area_square():\n    assert area_square(4) == 16\n",
        encoding="utf-8")
    (ws / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")


def _refactor_verify(ws: Path, result: Any) -> Dict[str, Any]:
    from src import verification
    v = verification.run_verifier("tests", str(ws), changed=["shapes.py"])
    ok = bool(v.get("ran")) and not v.get("inconclusive") and not v.get("new_failures") and v.get("ok") is True
    # A refactor that just deletes the duplication (instead of reusing it) is
    # not what was asked: the source must actually call the shared function.
    reused = "area_rectangle(s, s)" in (ws / "shapes.py").read_text(encoding="utf-8")
    return {"ok": ok and reused, "detail": v.get("summary"), "verification": v, "reused_shared_helper": reused}


REFACTOR = Task(
    name="refactor", category="refactor",
    message="area_square duplica la lógica de area_rectangle. Refactoriza para que area_square llame a "
            "area_rectangle(s, s) en vez de repetir s * s, sin cambiar el comportamiento.",
    script=[
        '```read_file\n{"path": "shapes.py"}\n```',
        '```edit_file\n{"path": "shapes.py", "old_string": "    return s * s", "new_string": "    return area_rectangle(s, s)"}\n```',
        "He eliminado la duplicación: area_square ahora reutiliza area_rectangle.",
    ],
    setup=_refactor_setup, verify=_refactor_verify,
)


# ---------------------------------------------------------------------------
# 4. Investigation over mocked (local) sources
# ---------------------------------------------------------------------------

_SOURCE_TEXT = (
    "Faustus es un fork de Odysseus. A fecha del lote 29, el mapa de "
    "reutilizacion listaba 23 requisitos P0 en estado 'existente' sobre un "
    "total de 100 requisitos P0 auditados.\n"
)


def _investigation_setup(ws: Path) -> None:
    (ws / "sources").mkdir(parents=True, exist_ok=True)
    (ws / "sources" / "doc1.txt").write_text(_SOURCE_TEXT, encoding="utf-8")


def _investigation_verify(ws: Path, result: Any) -> Dict[str, Any]:
    from src import verification
    report = ws / "report.md"
    if not report.is_file():
        return {"ok": False, "detail": "report.md was not produced"}
    text = report.read_text(encoding="utf-8")
    sentence = next((ln for ln in text.splitlines() if "23" in ln), "")
    if not sentence:
        return {"ok": False, "detail": "report.md never cites the figure from the source"}
    verdict = verification.verify_citation(sentence, _SOURCE_TEXT)
    return {"ok": verdict == "supported", "detail": f"verify_citation={verdict}", "citation_verdict": verdict}


INVESTIGATION = Task(
    name="investigation", category="investigation",
    message="Lee sources/doc1.txt y escribe report.md con un resumen de una frase que cite la cifra "
            "exacta de requisitos 'existente' que menciona la fuente.",
    script=[
        '```read_file\n{"path": "sources/doc1.txt"}\n```',
        '```write_file\n{"path": "report.md", "content": "# Informe\\n\\nA fecha del lote 29, 23 requisitos P0 estaban en estado existente sobre 100 auditados.\\n"}\n```',
        "He escrito report.md con la cifra citada de la fuente.",
    ],
    setup=_investigation_setup, verify=_investigation_verify,
)


# ---------------------------------------------------------------------------
# 5. Document
# ---------------------------------------------------------------------------

_REQUIRED_SECTIONS = ("# Purpose", "# Steps", "# Risks")


def _document_setup(ws: Path) -> None:
    pass


def _document_verify(ws: Path, result: Any) -> Dict[str, Any]:
    memo = ws / "memo.md"
    if not memo.is_file():
        return {"ok": False, "detail": "memo.md was not produced"}
    text = memo.read_text(encoding="utf-8")
    missing = [s for s in _REQUIRED_SECTIONS if s not in text]
    return {"ok": not missing, "detail": f"missing sections: {missing}" if missing else "all sections present"}


DOCUMENT = Task(
    name="document", category="document",
    message="Escribe memo.md, una nota de incorporación corta con las secciones Purpose, Steps y Risks "
            "(encabezados de nivel 1 en ese orden).",
    script=[
        '```write_file\n{"path": "memo.md", "content": "# Purpose\\nOnboard a new teammate.\\n\\n# Steps\\n1. Clone the repo.\\n2. Read the README.\\n\\n# Risks\\nMissing local model endpoint.\\n"}\n```',
        "He escrito memo.md con las tres secciones pedidas.",
    ],
    setup=_document_setup, verify=_document_verify,
)


# ---------------------------------------------------------------------------
# 6. Tabular data
# ---------------------------------------------------------------------------

_CSV_ROWS = ((1, 10), (2, 15), (3, 7))


def _tabular_setup(ws: Path) -> None:
    lines = ["id,amount"] + [f"{i},{a}" for i, a in _CSV_ROWS]
    (ws / "data.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tabular_verify(ws: Path, result: Any) -> Dict[str, Any]:
    total_file = ws / "total.txt"
    if not total_file.is_file():
        return {"ok": False, "detail": "total.txt was not produced"}
    expected = sum(a for _i, a in _CSV_ROWS)
    try:
        got = int(total_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return {"ok": False, "detail": "total.txt does not contain an integer"}
    return {"ok": got == expected, "detail": f"expected {expected}, got {got}"}


TABULAR = Task(
    name="tabular", category="tabular",
    message="Lee data.csv y escribe en total.txt la suma de la columna amount.",
    script=[
        '```read_file\n{"path": "data.csv"}\n```',
        f'```write_file\n{{"path": "total.txt", "content": "{sum(a for _i, a in _CSV_ROWS)}"}}\n```',
        "He escrito la suma de amount en total.txt.",
    ],
    setup=_tabular_setup, verify=_tabular_verify,
)


ALL_TASKS: List[Task] = [BUG_FIX, FEATURE, REFACTOR, INVESTIGATION, DOCUMENT, TABULAR]
