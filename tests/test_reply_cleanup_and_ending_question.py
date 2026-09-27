"""What a turn leaves behind when it ends: no leaked tool-call markup or blank
runs in the saved reply, and a time-limit question that says so (exam 32)."""
import json

from src.agent_loop import _end_turn_with_question
from src.tool_parsing import clean_reply_for_save


def test_leaked_qwen_call_and_blank_runs_are_cleaned():
    raw = ("Voy a revisar el workspace.\n\n<tool_call>\n<function=bash>\n<parameter=command>\n"
           "dir /b \"D:\\\\x\"\n</parameter>\n</function>\n</tool_call>\n\n\n\n\n\n\n\nSigo con el análisis.")
    out = clean_reply_for_save(raw)
    assert "<function" not in out and "<tool_call>" not in out and "\n\n\n" not in out
    assert out.startswith("Voy a revisar") and out.endswith("Sigo con el análisis.")


def test_code_fences_and_plain_text_stay():
    raw = "Ejemplo:\n\n```bash\nls -la\n```\n\nListo."
    assert clean_reply_for_save(raw) == raw
    assert clean_reply_for_save("") == ""


class _Ledger:
    def __init__(self, language, progress=()):
        self.language = language
        self.progress = list(progress)
        self.notes = []

    def mutated_paths(self):
        return []


def _question(ledger, **kw):
    for _kind, chunk in _end_turn_with_question(reason=kw.pop("reason"), round_num=kw.pop("round_num", 59),
                                               session_id=None, owner="", ledger=ledger, **kw):
        if chunk.startswith("data: "):
            data = json.loads(chunk[6:])
            if data.get("type") == "ask_user":
                return data["data"]["question"]
    raise AssertionError("no question")


def test_time_limit_question_says_the_time_ran_out():
    q = _question(_Ledger("es", [{"content": "aplicar las flechas", "status": "pending"}]),
                  reason="turn_wall_clock_ceiling", elapsed_s=10937.2)
    assert q.startswith("Se acabó el tiempo de este turno (182 min, 58 rondas)")
    assert "aplicar las flechas" in q and "No pude avanzar" not in q


def test_questions_follow_the_turn_language():
    assert _question(_Ledger("en"), reason="turn_wall_clock_ceiling").startswith("This turn ran out of time")
    assert _question(_Ledger("en"), reason="no_progress").startswith("I could not make progress")
    assert _question(_Ledger("es"), reason="no_progress").startswith("No pude avanzar")
