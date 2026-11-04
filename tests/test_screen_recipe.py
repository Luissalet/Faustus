"""Hangar E40: a procedure observed on screen (Talos draft) becomes a learned recipe from explicitly mapped steps."""
from __future__ import annotations

import pytest

from src import constants as constants_mod
from src import derivation_ops
from src import recipes
from src import screen_recipe as sr

OWNER = "alice"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def draft():
    return {"schema": sr.SCHEMA, "draft": True, "status": "draft_needs_verification", "executes_nothing": True, "title": "Registrar factura",
            "locale": "es", "window": {"start": "2026-11-04T10:00:00", "end": "2026-11-04T10:00:30", "duration_s": 30.0, "events": 5},
            "steps": [{"n": 1, "kind": "focus", "title": "Abrir Facturas.xlsx en Excel", "app": "Excel", "window": "Facturas.xlsx", "inputs": [], "unknowns": []},
                      {"n": 2, "kind": "copy_paste", "title": "Pasar el número de Excel al portal", "app": "Chrome", "window": "Portal",
                       "inputs": ["numero_de_factura"], "unknowns": []},
                      {"n": 3, "kind": "submit", "title": "Enviar el formulario del portal", "app": "Chrome", "window": "Portal", "inputs": [],
                       "unknowns": ["final_state_not_observed"]}],
            "inputs": [{"name": "numero_de_factura", "kind": "id", "source": "copied from Excel", "example": "F-2026-118"},
                       {"name": "clave", "kind": "text", "source": "typed", "example": "[masked]", "masked": True}],
            "outputs": [{"step": 2, "kind": "transferred_value", "description": "valor del paso 1 al 2", "value": "F-2026-118"}],
            "unknowns": [{"code": "final_state_not_observed", "step": 3, "message": "No se vio el estado final tras el paso 3."},
                         {"code": "intent_not_visible", "step": None, "message": "La intención no se ve en la grabación."}],
            "masked_values": 1, "shape": "abc123"}


MAPPING = [{"step": 1, "tool": "read_file", "args": {"path": "C:\\Users\\ana\\Facturas.xlsx"}},
           {"step": 2, "tool": "write_file", "args": {"path": "C:\\Users\\ana\\registro.txt", "content": "Factura F-2026-118"}}]


def test_mapped_steps_become_a_recipe_and_the_rest_become_limits():
    out = sr.propose(OWNER, draft(), MAPPING)
    r = out["recipe"]
    assert out["mapped_steps"] == [1, 2] and [m["n"] for m in out["manual_steps"]] == [3]
    assert [t["tool"] for t in r["step_templates"]] == ["read_file", "write_file"]
    examples = {str(p["example"]) for p in r["params"]}
    assert "F-2026-118" in examples                                         # the observed input is a parameter now
    assert "F-2026-118" not in str(r["step_templates"][1]["args"])          # and no longer a literal of the first case
    limits = " | ".join(r["limits"])
    assert "Step 3 stays manual" in limits and "No se vio el estado final" in limits and "masked" in limits
    assert r["source"]["kind"] == "screen" and r["source"]["manual_steps"] == [3]
    assert "[masked]" not in str(r)
    stored = recipes.get_recipe(r["id"], OWNER)
    assert stored is not None and stored.source["shape"] == "abc123"


def test_nothing_mapped_bad_steps_and_bad_drafts_are_refused():
    for mapping, cls in ((None, "screen.no_mapped_steps"), ([{"step": 9, "tool": "read_file", "args": {}}], "screen.bad_mapping"),
                         ([{"step": 1, "tool": "read_file", "args": "x"}], "screen.bad_mapping")):
        with pytest.raises(sr.ScreenRecipeError) as e:
            sr.propose(OWNER, draft(), mapping)
        assert e.value.error_class == cls
    with pytest.raises(sr.ScreenRecipeError) as e:
        sr.propose(OWNER, {"schema": "other"}, MAPPING)
    assert e.value.error_class == "screen.bad_draft"


def test_talos_is_asked_through_the_hub_and_a_disabled_module_is_explained(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, "_hub_call", lambda app, tool, args: (calls.append((app, tool, args)), {"ok": True, "result": draft()})[1])
    out = derivation_ops.run_recipe(OWNER, {"action": "screen_draft", "events": [{"ts": 1, "type": "focus", "app": "Excel"}], "title": "x"})
    assert calls[0][:2] == ("talos", "activity_procedure_draft") and calls[0][2]["events"] and out["summary"]["steps"][1]["inputs"] == ["numero_de_factura"]
    assert out["summary"]["inputs"][1]["masked"] is True
    made = derivation_ops.run_recipe(OWNER, {"action": "from_screen", "start": "-30m", "mapping": MAPPING, "title": "Factura al registro"})
    assert made["recipe"]["title"] == "Factura al registro" and calls[-1][2]["start"] == "-30m"
    monkeypatch.setattr(sr, "_hub_call", lambda *a: {"ok": False, "error": "module_disabled"})
    off = derivation_ops.run_recipe(OWNER, {"action": "screen_draft", "start": "-30m"})
    assert off["error_class"] == "screen.talos_unavailable" and "Activity module" in off["error"]


def test_the_recipe_is_tried_on_a_second_case():
    r = sr.propose(OWNER, draft(), MAPPING)["recipe"]
    params = {p["name"]: p["example"] for p in r["params"]}
    second = {k: ("F-2026-207" if str(v) == "F-2026-118" else (str(v).replace("ana", "luis") if isinstance(v, str) else v)) for k, v in params.items()}
    tried = derivation_ops.run_recipe(OWNER, {"action": "try", "recipe_id": r["id"], "params": second})
    assert not tried.get("error"), tried
    assert "F-2026-118" not in str(tried.get("steps") or tried)

def test_an_own_recipe_can_be_deleted_and_built_ins_cannot():
    rid = sr.propose(OWNER, draft(), MAPPING)["recipe"]["id"]
    gone = derivation_ops.run_recipe(OWNER, {"action": "delete", "recipe_id": rid})
    assert gone["deleted"] == rid and recipes.get_recipe(rid, OWNER) is None
    assert derivation_ops.run_recipe(OWNER, {"action": "delete", "recipe_id": rid})["error_class"] == "recipes.not_found"
    builtin = next(iter(recipes.list_recipes()), None)
    if builtin is not None:
        assert derivation_ops.run_recipe(OWNER, {"action": "delete", "recipe_id": builtin.id})["error_class"] == "recipes.not_found"
        assert recipes.get_recipe(builtin.id) is not None

def test_values_written_in_the_mapping_become_parameters_even_with_spaces():
    d = draft()
    d["inputs"] = [{"name": "clave", "kind": "secret", "source": "typed", "example": "[masked]", "masked": True}]
    mapping = [{"step": 2, "tool": "write_file", "args": {"path": "C:\\Users\\ana\\Mis facturas\\registro 2026.txt", "content": "ok"}}]
    r = sr.propose(OWNER, d, mapping)["recipe"]
    assert any("registro 2026.txt" in str(p["example"]) for p in r.get("params", [])), r.get("params")
