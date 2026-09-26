"""A per-turn cost ceiling in dollars replaces the spend-unit one."""
from src import autonomy_budget as ab


def _gs(values):
    return lambda key, default=None: values.get(key, default)


def test_usd_ceiling_replaces_the_preset_spend_ceiling():
    base = ab.resolve_budget("bounded_autonomous", get_setting=_gs({}))
    assert base.max_remote_spend == 60_000.0  # 20k units x 3
    b = ab.resolve_budget("bounded_autonomous", get_setting=_gs({"agent_turn_max_cost_usd": 2.0}))
    assert b.max_remote_spend == round(2.0 / ab.USD_PER_UNIT, 1)


def test_a_turn_stops_at_the_dollar_ceiling():
    b = ab.resolve_budget("supervised", get_setting=_gs({"agent_turn_max_cost_usd": 1.0}))
    led = ab.Ledger()
    led.add_remote_spend(ab.remote_spend_units(endpoint_cost_tracked=True, input_tokens=0, output_tokens=0,
                                               provider_cost_usd=0.6))
    assert led.check(b.without_tool_calls()) is None
    led.add_remote_spend(ab.remote_spend_units(endpoint_cost_tracked=True, input_tokens=0, output_tokens=0,
                                               provider_cost_usd=0.5))
    ex = led.check(b.without_tool_calls())
    assert ex is not None and ex.kind == "remote_spend"
