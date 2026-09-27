from src.agent_loop import _ledger_recurring_tools


def _tool(server="Ledger sintético", disabled=False, qualified="mcp__ledger__recurring_candidates"):
    return {"server_name": server, "name": "recurring_candidates",
            "qualified_name": qualified, "is_disabled": disabled}


def test_ledger_recurring_route_offers_one_matching_tool():
    for query in ("Qué pagos son recurrentes", "Han subido de precio mis suscripciones", "Recibos periódicos"):
        assert _ledger_recurring_tools(query, [_tool()]) == {"mcp__ledger__recurring_candidates"}


def test_ledger_recurring_route_does_not_guess_server():
    assert _ledger_recurring_tools("Pagos recurrentes", []) == set()
    assert _ledger_recurring_tools("Pagos recurrentes", [_tool(disabled=True)]) == set()
    assert _ledger_recurring_tools("Pagos recurrentes", [_tool(server="Other")]) == set()
    assert _ledger_recurring_tools("Pagos recurrentes", [_tool(), _tool(qualified="mcp__other__recurring_candidates")]) == set()
    assert _ledger_recurring_tools("Saldo de la cuenta", [_tool()]) == set()
