"""An answer may only cite result ids a tool actually returned."""
from src.agent_harness import TurnLedger

CALC = {"output": '{"id": "L-000005", "cite": "[L-000005]", "exact": "364"}', "exit_code": 0}


def _ledger():
    led = TurnLedger(None, "Revisa la fila de horas de tu tabla")
    led.record("mcp__lap__calc", '{"expression": "56 * 6.5"}', CALC, 1)
    return led


def test_invented_ids_are_flagged():
    led = _ledger()
    body = "Horas: 364 [L-000005]. Base: 15.470,00 € [L-000006]. Total: 18.718,70 € [L-000008]."
    check = led.check_completion(body)
    assert "fabricated_citations" in check["reasons"]
    assert check["bad_citations"] == ["L-000006", "L-000008"]
    msg = led.rejection_message(check)
    assert "[L-000006]" in msg and "do NOT redo it" in msg
    assert "cita resultados" in led.user_note(check, final=True)


def test_ids_from_the_conversation_are_known():
    led = _ledger()
    led.note_known_text("Días laborables: 56 [L-000004]")
    assert led.check_completion("56 días [L-000004], 364 h [L-000005].")["ok"]


def test_no_citing_tool_in_play_means_no_check():
    led = TurnLedger(None, "hola")
    led.record("read_file", '{"path": "a.md"}', {"output": "texto", "exit_code": 0}, 1)
    assert "fabricated_citations" not in led.check_completion("Ver ticket ABC-123456 en el tracker.")["reasons"]


def test_other_prefixes_are_not_judged():
    led = _ledger()
    # a Nightingale id when only a calculator ran: not this ledger's business
    assert "fabricated_citations" not in led.check_completion("364 [L-000005]; ver N-000123")["reasons"]
