"""How a sentence names an installed plugin (src/plugins.named_in).

Tool selection and the approval gate both ask this question, so they share
one answer. Uses the manifests shipped in plugins/.
"""
import pytest

from src.plugins import fold, named_in


def _ids(text):
    return sorted(p.id for p in named_in(text))


@pytest.mark.parametrize("text,expected", [
    ("Arranca Jobhunter's Hoard y dime si responde.", ["jobhunter"]),
    ("abre jobhunter", ["jobhunter"]),
    ("¿Qué tal va el JOBHUNTER?", ["jobhunter"]),
    ("Pásale esto a Writer's Hoard", ["writer"]),
    ("¿Qué aplicaciones mías puedes usar?", []),
    ("jobhunting is hard", []),
])
def test_named_in(text, expected):
    assert _ids(text) == expected


def test_fold_drops_accents_case_and_apostrophes():
    assert fold("Enséñame Jobhunter’s  HOARD!") == "ensename jobhunters hoard"
