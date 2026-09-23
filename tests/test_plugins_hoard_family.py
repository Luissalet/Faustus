"""The Hoard apps that ship as plugins (ledger, links, people, argus, borges,
scribe, vulcan, hypatia, echo).

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
    "scribe": ("scribe-hoard", 5185, "scribe"),
    "vulcan": ("vulcan-hoard", 5186, "vulcan"),
    "hypatia": ("hypatia-hoard", 5187, "hypatia"),
    "echo": ("echo-hoard", 5188, "echo"),
}


def test_the_family_ships_and_loads_clean():
    loaded = plugins.load_all()
    assert loaded.errors == [], loaded.errors
    assert set(FAMILY) <= set(loaded.plugins)


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


def test_the_family_ports_do_not_collide_with_each_other_or_with_the_older_plugins():
    urls = {}
    for pid, plugin in plugins.load_plugins().items():
        url = plugin.defaults.get("APP_URL")
        if url:
            assert url not in urls, f"{pid} and {urls[url]} share {url}"
            urls[url] = pid


@pytest.mark.parametrize("pid", sorted(FAMILY))
def test_a_person_can_name_each_member(pid):
    _service, _port, word = FAMILY[pid]
    named = {p.id for p in plugins.named_in(f"abre {word} y dime si responde")}
    assert pid in named
