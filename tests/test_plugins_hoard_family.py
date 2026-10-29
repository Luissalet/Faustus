"""The Hoard apps that ship as plugins (ledger, links, people, argus, borges,
vulcan, hypatia, echo, nightingale, cassandra, vitruvius, midas, cicero, tantalus, lumiere,
phileas, kafka, galton, pygmalion, prometheus).

Each is a standalone application with its own repository; what ships here is
Faustus's side of the contract, copied from the `faustus-plugin.json` the app
carries. These tests pin what makes each one usable without hand
configuration: it loads, a scan can recognise it, it can be started from a
profile, its MCP bridge is declared, and a person can name it in a sentence.
"""
from __future__ import annotations

import pytest

from src import plugins

FAMILY = {
    # id: (service in /api/health, default port, first word a person would say)
    "ledger": ("ledgers-hoard", 5180, "ledger"),
    "links": ("links-hoard", 5181, "links"),
    "people": ("peoples-hoard", 5182, "people"),
    "argus": ("argus-hoard", 5183, "argus"),
    "borges": ("borges-hoard", 5184, "borges"),
    "vulcan": ("vulcan-hoard", 5186, "vulcan"),
    "hypatia": ("hypatia-hoard", 5187, "hypatia"),
    "echo": ("echo-hoard", 5188, "echo"),
    "nightingale": ("nightingale-hoard", 5189, "nightingale"),
    "cassandra": ("cassandra-hoard", 5190, "cassandra"),
    "vitruvius": ("vitruvius-hoard", 5191, "vitruvius"),
    "midas": ("midas-hoard", 5192, "midas"),
    "cicero": ("cicero-hoard", 5194, "cicero"),
    "lumiere": ("lumiere-hoard", 5198, "lumiere"),
    "kafka": ("kafka-hoard", 5200, "kafka"),
    "galton": ("galton-hoard", 5201, "galton"),
    "pygmalion": ("pygmalion-hoard", 5202, "pygmalion"),
    "atlas": ("atlas-hoard", 5203, "atlas"),
    "heron": ("herons-hoard", 5204, "heron"),
    "prometheus": ("prometheus-hoard", 5205, "prometheus"),
}


#: Hoards folded into another app. Their plugin is the owner's compatibility
#: identity (same id, so existing connectors and names keep working): it
#: starts the owner's bridge, lives under the owner's address and has no
#: server of its own to launch.
ABSORBED = {
    # id: (service in /api/health, owner's address, first word a person would say)
    "tantalus": ("tantalus-hoard", "http://127.0.0.1:5221/products", "tantalus"),
    "phileas": ("phileas-hoard", "http://127.0.0.1:5221/shipments", "phileas"),
}


def test_the_family_ships_and_loads_clean():
    loaded = plugins.load_all()
    assert loaded.errors == [], loaded.errors
    assert set(FAMILY) | set(ABSORBED) <= set(loaded.plugins)
    assert "scribe" not in loaded.plugins


@pytest.mark.parametrize("pid", sorted(FAMILY))
def test_each_member_is_recognisable_startable_and_bridged(pid):
    service, port, _word = FAMILY[pid]
    plugin = plugins.get(pid)
    assert plugin is not None
    assert service in plugin.identify.get("service", []), plugin.identify
    assert plugin.defaults.get("APP_URL") == f"http://127.0.0.1:{port}"
    assert plugin.ui_url == "{APP_URL}", "every Hoard app is a page, not a bare API"
    assert plugin.launch_hint and plugin.launch_hint.get("readiness"), "no profile can be offered without a launch hint"
    assert plugin.command and plugin.args, "the MCP bridge is what lends the tools"
    assert plugin.purpose, "plugins_list shows the purpose; an empty one leaves the model guessing"


@pytest.mark.parametrize("pid", sorted(ABSORBED))
def test_an_absorbed_member_is_recognisable_and_bridged_through_its_owner(pid):
    service, url, _word = ABSORBED[pid]
    plugin = plugins.get(pid)
    assert plugin is not None
    assert service in plugin.identify.get("service", []), plugin.identify
    assert plugin.defaults.get("APP_URL") == url
    assert plugin.placeholders[0] == "HERMES_DIR"
    assert "HERMES_DIR" not in plugin.defaults, "the owner's folder is a value, never a guess"
    assert plugin.args == ["{HERMES_DIR}/compatibility/mcp_server.py"]
    assert plugin.env["HOARD_COMPAT_ID"] == pid
    assert not plugin.launch_hint, "an absorbed Hoard has no server of its own to start"
    assert plugin.purpose


def test_the_family_ports_do_not_collide_with_each_other_or_with_the_older_plugins():
    urls = {}
    for pid, plugin in plugins.load_plugins().items():
        url = plugin.defaults.get("APP_URL")
        if url:
            assert url not in urls, f"{pid} and {urls[url]} share {url}"
            urls[url] = pid


@pytest.mark.parametrize("pid", sorted(set(FAMILY) | set(ABSORBED)))
def test_a_person_can_name_each_member(pid):
    word = (FAMILY.get(pid) or ABSORBED[pid])[2]
    named = {p.id for p in plugins.named_in(f"abre {word} y dime si responde")}
    assert pid in named


def test_midas_declares_its_bridge_environment_and_is_not_in_the_public_catalogue():
    import json
    from pathlib import Path

    plugin = plugins.get("midas")
    assert plugin is not None
    assert plugin.name == "Midas's Hoard"
    assert {"MIDAS_DIR", "APP_URL", "TOKEN_FILE", "PYTHON"} <= set(plugin.placeholders)
    assert plugin.launch_hint["env"]["MIDAS_PORT"] == "5192"
    assert "midas_hoard" in plugin.launch_hint["argv"]
    assert any(arg.endswith("mcp_server.py") for arg in plugin.args)
    assert "backtest" in plugin.capabilities
    # No public repository yet: it must not appear in the clonable catalogue.
    catalog = json.loads((Path(__file__).parents[1] / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    assert "midas" not in {entry["id"] for entry in catalog["plugins"]}
