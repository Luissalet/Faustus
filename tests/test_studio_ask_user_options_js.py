"""The model's question reaches the screen with readable options.

ask_user sends options as {label, description}; the live event path used to
String() them, so every button read "[object Object]" (10-09-2026). The card
now shows one button per option (with its consequence), a checklist when the
tool says `multi`, and always a free-text line — the shape Luis asked for
("varias opciones para elegir y una para que escribas tú, o una checklist").
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required")
def test_ask_user_options_are_labels_not_object_strings():
    result = subprocess.run(["node", "studio/checks/ask-user-options.check.mjs"], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok ask-user-options" in result.stdout


def test_the_card_offers_buttons_a_checklist_and_a_free_answer():
    source = (ROOT / "studio/src/screens/studio/Transcript.tsx").read_text(encoding="utf-8")
    card = source[source.index("function QuestionCard("):source.index("export function AnsweredCard(")]
    assert 'data-testid="studio-question-option"' in card          # one button per option
    assert 'type="checkbox"' in card and "ask.multi" in card         # a checklist when multi
    assert 'data-testid="studio-question-own"' in card              # always a line of your own
    assert "option.description" in card                             # the consequence under the label
    assert "picked.join('; ')" in card                              # several picks travel as one answer


def test_the_tool_tells_the_model_when_to_ask_and_not_to_invent_other():
    source = (ROOT / "src/agent_loop.py").read_text(encoding="utf-8")
    line = next(l for l in source.splitlines() if l.strip().startswith('"ask_user":'))
    assert "implement X" in line and "recommended" in line
    assert "multi" in line and "checklist" in line
    assert 'never add an \\"Other\\" option' in line
