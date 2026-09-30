"""H17: exposure (direct / deferred / code-only) is a descriptor field the runtime reads."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import src.agent_tools  # noqa: F401  (registers the built-in tools)
from src import tool_exposure as exposure
from src import tool_serve
from src.tool_authority import AUTHORITY, Exposure, ParserContract, ToolAuthority, make_tool

LOOP = Path(__file__).parents[1] / "src" / "agent_loop.py"


# -- the field ---------------------------------------------------------------------
def test_exposure_is_a_field_of_every_descriptor_and_defaults_to_direct():
    exposures = AUTHORITY.exposure_map()
    assert exposures and set(exposures.values()) <= set(Exposure)
    assert exposures["read_file"] is Exposure.DIRECT
    assert exposures["board_list"] is Exposure.DEFERRED
    assert AUTHORITY.exposure("no_such_tool") is Exposure.DIRECT


def test_the_descriptor_reports_its_exposure_in_the_admin_view():
    assert AUTHORITY.get("board_list").summary()["exposure"] == "deferred"


# -- the evidence rule ---------------------------------------------------------------
@pytest.mark.parametrize("query,tool", [
    ("what's open on this project's board", "board_list"),
    ("qué issues hay pendientes en este proyecto", "board_list"),
    ("apunta un bug sobre el botón de pago roto", "board_create"),
    ("move that issue to in progress", "board_update"),
])
def test_a_request_that_speaks_of_the_tool_is_evidence_for_it(query, tool):
    assert exposure.has_evidence(tool, query)


@pytest.mark.parametrize("query", ["hola, qué tal", "reads the file server.py", "send an email to Marta", ""])
def test_a_request_about_something_else_is_no_evidence(query):
    assert not exposure.has_evidence("board_list", query)


def test_a_word_every_tool_uses_is_not_evidence():
    frequency = exposure._document_frequency()
    assert any(n > exposure.RARE_DOCUMENT_FREQUENCY for n in frequency.values()), "the example statistics are empty"
    # "project" and "tarea" style words are in many tools' examples: they do not keep a tool native
    assert not exposure.has_evidence("board_list", "please do this for the project")


# -- demotion --------------------------------------------------------------------------
SEED = {"read_file", "board_list", "code_graph_search", "swarm_start"}


def test_a_deferred_tool_without_evidence_is_demoted_and_a_direct_one_never():
    stay, demoted = exposure.demote(SEED, "lee el fichero server.py", candidates=set(SEED))
    assert "read_file" in stay
    assert demoted and demoted <= {"board_list", "code_graph_search", "swarm_start"}
    assert not demoted & stay and (stay | demoted) == SEED


def test_a_deferred_tool_the_request_speaks_of_stays_native():
    stay, demoted = exposure.demote(SEED, "what's open on this project's board", candidates=set(SEED))
    assert "board_list" in stay and "board_list" not in demoted


def test_forced_and_non_retrieval_tools_are_never_demoted():
    stay, demoted = exposure.demote(SEED, "hola", candidates=set(SEED), keep={"board_list"})
    assert "board_list" in stay
    stay, demoted = exposure.demote(SEED, "hola", candidates={"code_graph_search"})
    assert "board_list" in stay and "swarm_start" in stay and "code_graph_search" in demoted


def test_a_code_only_tool_leaves_the_seed_without_becoming_a_catalog_entry(monkeypatch):
    registry = ToolAuthority()
    registry.register(make_tool(
        name="bridge_only", canonical_id="probe.bridge_only", family="probe", description="d",
        parameters={"type": "object", "properties": {}}, parser=ParserContract(lambda a: ""),
        exposure=Exposure.CODE_ONLY))
    monkeypatch.setattr("src.tool_authority.AUTHORITY", registry)
    stay, demoted = exposure.demote({"bridge_only"}, "bridge only", candidates={"bridge_only"})
    assert stay == set() and demoted == set()
    assert exposure.code_only_names(["bridge_only", "read_file"]) == {"bridge_only"}


def test_a_direct_model_call_to_a_code_only_tool_is_refused_but_a_program_call_is_not(monkeypatch):
    from src.step_snapshot import authorize_call, capture_step
    registry = ToolAuthority()
    registry.register(make_tool(
        name="bridge_only", canonical_id="probe.bridge_only", family="probe", description="d",
        parameters={"type": "object", "properties": {}}, parser=ParserContract(lambda a: ""),
        exposure=Exposure.CODE_ONLY))
    monkeypatch.setattr("src.tool_authority.AUTHORITY", registry)
    snapshot = capture_step([], session_id="s", run_id="r", round_num=1, candidate_index=0, owner="o")
    refused = authorize_call(snapshot, "bridge_only", session_id="s", owner="o")
    assert not refused.allowed and refused.status == "code_only"
    assert authorize_call(snapshot, "bridge_only", session_id="s", owner="o", mode="shadow").allowed
    bridge = authorize_call(None, "bridge_only")
    assert bridge.allowed and bridge.status == "no_snapshot"


def test_discovery_never_proposes_a_code_only_tool(monkeypatch):
    monkeypatch.setattr(tool_serve, "_code_only", lambda name: name == "bridge_only")
    monkeypatch.setattr(tool_serve, "resolve_bare_name", lambda name: [name])
    assert tool_serve.search_catalog("", ["bridge_only", "read_file"]) == ["read_file"]


# -- the measurement on the tool-index benchmark ----------------------------------------------
def _measure():
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS, ToolIndex, _examples_block
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_slimming import _tokens
    from tests.test_l91_natural_language_tools import _registered_cases

    index = ToolIndex.__new__(ToolIndex)
    index._set_corpus("builtin", {n: f"Tool: {n}\n{d}" + _examples_block(n) for n, d in BUILTIN_TOOL_DESCRIPTIONS.items()})
    schema = {e["function"]["name"]: e for e in FUNCTION_TOOL_SCHEMAS}
    cases = _registered_cases()
    before_tokens = after_tokens = reachable = native_before = native_after = 0
    demoted_total = 0
    for query, expected in cases:
        offered = set(index.lexical_retrieve(query, k=8))
        stay, demoted = exposure.demote(offered, query, candidates=offered)
        before_tokens += _tokens([schema[n] for n in offered if n in schema])
        after_tokens += _tokens([schema[n] for n in stay if n in schema])
        demoted_total += len(demoted)
        reachable += expected in offered            # native or listed in the catalog
        native_before += expected in offered
        native_after += expected in stay
    return dict(cases=len(cases), before=before_tokens, after=after_tokens, reachable=reachable,
                native_before=native_before, native_after=native_after, demoted=demoted_total)


def test_exposure_cuts_schema_tokens_without_losing_selection_accuracy():
    m = _measure()
    assert m["cases"] >= 100
    assert m["native_before"] >= 0.9 * m["cases"]
    # selection accuracy, strictest reading: the expected tool still carries its schema
    assert m["native_after"] >= m["native_before"], m
    # reachability: every retrieved tool is still native or in the catalog
    assert m["reachable"] == m["native_before"]
    # and the point of it
    assert m["demoted"] > 0
    assert m["after"] <= 0.85 * m["before"], m


# -- beyond the vector lane's window and legacy adapters ---------------------------------------------
class _FilteringIndex:
    """An index whose vector lane only sees the first 256 rows, like the real one."""

    def __init__(self, ranking, lexical):
        self.ranking = ranking
        self.lexical = lexical

    def retrieve(self, query, k=8, *, candidate_filter=None):
        window = self.ranking[:256]
        return [n for n in window if candidate_filter is None or candidate_filter(n)][:k]

    def lexical_retrieve(self, query, k=8, *, candidate_filter=None):
        return [n for n in self.lexical if candidate_filter is None or candidate_filter(n)][:k]


class _LegacyIndex:
    """An adapter from before permission-aware retrieval: no candidate_filter."""

    def __init__(self, ranking):
        self.ranking = ranking

    def retrieve(self, query, k=8):
        return self.ranking[:k]


def _install(monkeypatch, index, enabled=True):
    import src.tool_index as tool_index
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: index)
    monkeypatch.setattr(tool_serve, "_completion_on", lambda: enabled)
    monkeypatch.setattr(tool_serve, "_keyword_hits", lambda q: [])


def test_a_permitted_tool_past_the_256_row_window_is_reachable(monkeypatch):
    ranking = [f"denied_{i}" for i in range(300)] + ["wanted_tool"]
    index = _FilteringIndex(ranking, lexical=["wanted_tool"])
    allow = lambda name: name == "wanted_tool"  # noqa: E731
    _install(monkeypatch, index)
    assert tool_serve.search_catalog("the wanted thing", candidate_filter=allow) == ["wanted_tool"]
    _install(monkeypatch, index, enabled=False)
    assert tool_serve.search_catalog("the wanted thing", candidate_filter=allow) == []


def test_a_legacy_adapter_keeps_its_explicit_cutoff_and_is_completed_from_the_permitted_pool(monkeypatch):
    requests = []

    class Legacy(_LegacyIndex):
        def retrieve(self, query, k=8):
            requests.append(k)
            return super().retrieve(query, k)

        def lexical_retrieve(self, query, k=8, *, candidate_filter=None):
            return [n for n in self.ranking if candidate_filter is None or candidate_filter(n)][:k]

    ranking = [f"denied_{i}" for i in range(120)] + ["wanted_a", "wanted_b"]
    allow = lambda name: name.startswith("wanted")  # noqa: E731
    _install(monkeypatch, Legacy(ranking))
    assert tool_serve.search_catalog("anything", candidate_filter=allow) == ["wanted_a", "wanted_b"]
    assert requests == [8], "the adapter's cutoff is not inflated"
    _install(monkeypatch, Legacy(ranking), enabled=False)
    assert tool_serve.search_catalog("anything", candidate_filter=allow) == []


def test_a_legacy_adapter_without_a_lexical_lane_is_left_as_it_was(monkeypatch):
    requests = []

    class Bare:
        def retrieve(self, query, k=8):
            requests.append(k)
            return ["denied_1"]
    _install(monkeypatch, Bare())
    assert tool_serve.search_catalog("anything", k=3, candidate_filter=lambda n: False) == []
    assert requests == [3]


def test_completion_does_not_add_anything_when_the_index_already_filled_the_page(monkeypatch):
    ranking = [f"ok_{i}" for i in range(20)]
    index = _FilteringIndex(ranking, lexical=["lexical_extra"])
    _install(monkeypatch, index)
    found = tool_serve.search_catalog("x", k=5, candidate_filter=lambda n: True)
    assert found == ranking[:5]


def test_completion_never_returns_what_the_filter_refuses(monkeypatch):
    index = _FilteringIndex(["open_tool"], lexical=["secret_tool", "open_tool"])
    _install(monkeypatch, index)
    found = tool_serve.search_catalog("x", candidate_filter=lambda n: n != "secret_tool")
    assert found == ["open_tool"]


# -- the loop reads exposure ---------------------------------------------------------------------------
def test_the_loop_partitions_its_seed_through_the_exposure_module():
    source = LOOP.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "_exposure"}
    assert {"demote", "code_only_names"} <= called
    assert 'get_setting("agent_tool_exposure", True)' in source
    assert source.count("_exposure_pool = set(_relevant_tools)") == 2, "both retrieval paths must feed the pool"


def test_the_setting_exists_with_its_safe_default():
    from src.agent_settings_schema import schema_fields
    from src.settings import DEFAULT_SETTINGS
    keys = {f.get("key") for group in schema_fields() for f in group.get("fields", [group])}
    assert DEFAULT_SETTINGS["agent_tool_exposure"] is True
    assert "agent_tool_exposure" in keys


def test_the_legacy_adapters_the_loop_calls_keep_their_signatures():
    import inspect
    params = inspect.signature(tool_serve.search_catalog).parameters
    assert {"query", "names", "k", "disabled", "admin", "candidate_filter"} <= set(params)
    assert json.dumps(sorted(Exposure.__members__)) == '["CODE_ONLY", "DEFERRED", "DIRECT"]'
