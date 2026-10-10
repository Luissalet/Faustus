"""What a turn leaves behind when it ends: no leaked tool-call markup or blank
runs in the saved reply, and a time-limit question that says so (exam 32)."""
import json

from src.agent_loop import _end_turn_with_advisory
from src.tool_parsing import clean_reply_for_save


def test_leaked_qwen_call_and_blank_runs_are_cleaned():
    raw = ("Voy a revisar el workspace.\n\n<tool_call>\n<function=bash>\n<parameter=command>\n"
           "dir /b \"D:\\\\x\"\n</parameter>\n</function>\n</tool_call>\n\n\n\n\n\n\n\nSigo con el análisis.")
    out = clean_reply_for_save(raw)
    assert "<function" not in out and "<tool_call>" not in out and "\n\n\n" not in out
    assert out.startswith("Voy a revisar") and out.endswith("Sigo con el análisis.")


def test_the_same_narration_repeated_round_after_round_is_kept_once():
    raw = "\n\n".join(["Voy a mirar el workspace."] * 10 + ["Hecho."])
    assert clean_reply_for_save(raw) == "Voy a mirar el workspace.\n\nHecho."
    apart = "Uno.\n\nDos.\n\nUno."
    assert clean_reply_for_save(apart) == apart


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


def _notice(ledger, **kw):
    for _kind, chunk in _end_turn_with_advisory(reason=kw.pop("reason"), round_num=kw.pop("round_num", 59),
                                               session_id=None, owner="", ledger=ledger, **kw):
        if chunk.startswith("data: "):
            data = json.loads(chunk[6:])
            assert data.get("type") != "ask_user"
            if data.get("type") == "harness_check" and data.get("status") == "advisory":
                return data["detail"]
    raise AssertionError("no notice")


def test_time_limit_notice_reports_pending_work_without_requesting_permission():
    q = _notice(_Ledger("es", [{"content": "aplicar las flechas", "status": "pending"}]),
                  reason="turn_wall_clock_ceiling", elapsed_s=10937.2)
    assert q.startswith("Se acabó el tiempo de este turno (182 min, 58 rondas)")
    assert "aplicar las flechas" in q and "No pude avanzar" not in q


def test_notices_follow_the_turn_language():
    assert _notice(_Ledger("en"), reason="turn_wall_clock_ceiling").startswith("This turn ran out of time")
    assert _notice(_Ledger("en"), reason="no_progress").startswith("The turn ended without verifying")
    assert _notice(_Ledger("es"), reason="no_progress").startswith("El turno terminó sin acreditar")
