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


# ---------------------------------------------------------------------------
# faustus#2292: suggested mapping, real execution in an office file, several observations, Baird as a source
# ---------------------------------------------------------------------------

def _step(n, kind, app, window, inputs=(), title=None, **extra):
    return {"n": n, "kind": kind, "title": title or f"{kind} {app}", "app": app, "window": window, "inputs": list(inputs),
            "unknowns": [], **extra}


def office_draft(number="F-2026-118", window="Registro.xlsx - Excel", provider="ACME", extra_steps=()):
    steps = [_step(1, "app_switch", "Excel", window),
             _step(2, "fill_form", "Excel", window, ["numero"]),
             _step(3, "app_switch", "Bloc de notas", "registro.txt - Bloc de notas"),
             _step(4, "copy_paste", "Bloc de notas", "registro.txt - Bloc de notas", ["nota", "proveedor"]),
             _step(5, "fill_form", "Bloc de notas", "registro.txt - Bloc de notas", ["clave"]),
             _step(6, "file", "Bloc de notas", "registro.txt - Bloc de notas"), *extra_steps]
    return {"schema": sr.SCHEMA, "title": "Registrar factura", "locale": "es", "window": {"events": 9}, "steps": steps,
            "inputs": [{"name": "numero", "step": 2, "label": "A2", "source": "typed", "kind": "id", "example": number},
                       {"name": "nota", "step": 4, "label": None, "source": "copied from Excel", "kind": "id", "example": number},
                       {"name": "proveedor", "step": 4, "label": "Proveedor", "source": "typed", "kind": "name", "example": provider},
                       {"name": "clave", "step": 5, "label": "Clave", "source": "typed", "kind": "secret", "example": "[masked]", "masked": True}],
            "outputs": [], "masked_values": 1, "shape": "s-" + str(len(steps)),
            "unknowns": [{"code": "single_run", "step": None, "message": "Solo se observó una ejecución."}]}


def test_tools_are_suggested_from_the_app_and_the_kind_of_step():
    got = sr.suggest_mapping(office_draft())
    by = {r["step"]: r for r in got["suggestions"]}
    assert by[1]["tool"] == "manage_spreadsheet" and by[1]["args"] == {"action": "read_workbook", "path": "Registro.xlsx"}
    assert by[2]["args"] == {"action": "write_range", "path": "Registro.xlsx", "sheet_name": "Hoja1", "start_cell": "A2",
                             "values": [["F-2026-118"]]} and "confirm sheet_name" in by[2]["why"]
    assert by[3]["action"] == "skip" and by[6]["action"] == "skip"
    assert by[4]["tool"] == "write_file" and by[4]["args"] == {"path": "registro.txt", "content": "F-2026-118"}
    assert by[5]["action"] == "manual" and "secret" in by[5]["why"]              # a secret is never put in a recipe
    assert [m["step"] for m in got["mapping"]] == [1, 2, 4] and got["counts"] == {"map": 3, "skip": 2, "manual": 1}
    other = {"schema": sr.SCHEMA, "title": "x", "steps": [_step(1, "fill_form", "Microsoft Word", "Carta.docx - Word", []),
                                                         _step(2, "submit", "Chrome", "Portal"), _step(3, "app_switch", "Edge", "https://example.org/alta - Edge"),
                                                         _step(4, "type_text", "Chrome", "Portal", [])], "inputs": []}
    rows = {r["step"]: r for r in sr.suggest_mapping(other)["suggestions"]}
    assert rows[1]["action"] == "manual" and ".docx" in rows[1]["why"]
    assert rows[2]["action"] == "manual" and rows[4]["action"] == "manual"
    assert rows[3]["tool"] == "browser_extract" and rows[3]["args"]["url"] == "https://example.org/alta"
    assert sr.app_family("Calculadora", "") == "other" and sr.file_in("Informe final.xlsx - Excel") == "Informe final.xlsx"


def _workbook(path, rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Hoja1"
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_the_suggested_recipe_runs_for_real_on_a_second_workbook_and_its_output_is_checked(tmp_path, monkeypatch):
    """Excel -> Bloc de notas seen on screen; the suggested mapping becomes a recipe that writes a second case's invoice
    into a real .xlsx (manage_spreadsheet) and a .txt (write_file) through the normal dispatcher, then exit criteria
    open the workbook and the text file. Nothing of the first case is left in what was delivered."""
    import json as _json
    monkeypatch.setenv("AUTH_ENABLED", "false")
    ws = tmp_path / "ws"
    ws.mkdir()
    _workbook(ws / "Registro.xlsx", [["factura"]])
    _workbook(ws / "Registro-dic.xlsx", [["factura"]])
    sug = sr.suggest_mapping(office_draft())
    made = derivation_ops.run_recipe(OWNER, {"action": "from_screen", "draft": office_draft(), "mapping": sug["mapping"],
                                             "title": "Factura al registro"})
    r = made["recipe"]
    names = {str(p["example"]): p["name"] for p in r["params"]}
    assert {"F-2026-118", "Registro.xlsx", "registro.txt"} <= set(names), r["params"]
    case = {names["F-2026-118"]: "F-2026-207", names["Registro.xlsx"]: "Registro-dic.xlsx", names["registro.txt"]: "registro-dic.txt"}
    alt = lambda v: v + 1 if isinstance(v, (int, float)) and not isinstance(v, bool) else f"{v}-b"  # noqa: E731
    case.update({p["name"]: alt(p["example"]) for p in r["params"] if p["name"] not in case})
    crit = [{"kind": "file_exists", "path": "registro-dic.txt"},
            {"kind": "contains", "path": "registro-dic.txt", "text": "F-2026-207"},
            {"kind": "contains", "path": "Registro-dic.xlsx", "text": "F-2026-207"},
            {"kind": "row_count", "path": "Registro-dic.xlsx", "sheet": "Hoja1", "equals": 1},
            {"kind": "columns", "path": "Registro-dic.xlsx", "equals": ["factura"]}]
    out = derivation_ops.run_recipe(OWNER, {"action": "execute", "recipe_id": r["id"], "params": case, "workspace": str(ws),
                                            "criteria": crit})
    assert out["executed"] is True and out["verified"] is True, _json.dumps(out, default=str)[:3000]
    assert [s["tool"] for s in out["steps"]] == ["manage_spreadsheet", "manage_spreadsheet", "write_file"]
    import openpyxl
    cells = [c.value for c in openpyxl.load_workbook(ws / "Registro-dic.xlsx")["Hoja1"]["A"]]
    assert cells == ["factura", "F-2026-207"]
    assert (ws / "registro-dic.txt").read_text(encoding="utf-8") == "F-2026-207"
    assert [c.value for c in openpyxl.load_workbook(ws / "Registro.xlsx")["Hoja1"]["A"]] == ["factura"]   # the first case is untouched
    wrong = derivation_ops.run_recipe(OWNER, {"action": "execute", "recipe_id": r["id"], "params": case, "workspace": str(ws),
                                              "criteria": [{"kind": "contains", "path": "Registro-dic.xlsx", "text": "F-2026-118"}],
                                              "rerun": True})
    assert wrong["verified"] is False and wrong["status"] == "criteria_failed"


def test_several_observations_make_parameters_of_what_changed_and_branches_of_extra_steps():
    first = office_draft("F-1", "Registro-enero.xlsx - Excel", "ACME")
    second = office_draft("F-2", "Registro-febrero.xlsx - Excel", "ACME",
                          extra_steps=[_step(7, "click", "Bloc de notas", "registro.txt - Bloc de notas", title="Pulsar Imprimir")])
    cmp = sr.compare_drafts([first, second])
    status = {i["name"]: i["status"] for i in cmp["inputs"]}
    assert status["numero"] == "varies" and status["proveedor"] == "constant" and status["clave"] == "masked"
    assert "window_step_1" in cmp["parameters"] and "window_step_3" in cmp["constants"]
    assert cmp["branches"] == [{"observation": 2, "kind": "click", "app": "Bloc de notas", "title": "Pulsar Imprimir", "after_step": 6}]
    assert all(s["seen_in"] == 2 for s in cmp["steps"]) and cmp["same_shape"] is False
    mapping = [{"step": 4, "tool": "write_file", "args": {"path": "registro.txt", "content": "Factura F-1 de ACME"}}]
    out = derivation_ops.run_recipe(OWNER, {"action": "from_screen", "draft": first, "observations": [second], "mapping": mapping})
    r = out["recipe"]
    examples = {str(p["example"]) for p in r["params"]}
    assert "F-1" in examples and "ACME" not in examples                     # constant across observations: stays literal
    assert "ACME" in str(r["step_templates"][0]["args"]) and "F-1" not in str(r["step_templates"][0]["args"])
    limits = " | ".join(r["limits"])
    assert "Learned from 2 observations" in limits and "Pulsar Imprimir" in limits and "Solo se observó una ejecución" not in limits
    assert r["source"]["observations"]["count"] == 2 and "numero" in r["source"]["observations"]["varying"]
    shown = derivation_ops.run_recipe(OWNER, {"action": "screen_draft", "draft": first, "observations": [{"draft": second}]})
    assert shown["comparison"]["observations"] == 2 and shown["suggestions"]["counts"]["map"] == 3
    with pytest.raises(sr.ScreenRecipeError):
        sr.compare_drafts([first])
    bad = derivation_ops.run_recipe(OWNER, {"action": "screen_draft", "draft": first, "observations": ["nope"]})
    assert bad["error_class"] == "screen.bad_observations"


def test_baird_screenshots_become_the_events_talos_segments(monkeypatch):
    from datetime import datetime as dt
    calls = []
    media = [{"name": "baird-10-05.png", "kind": "image", "modified": "2026-11-04T10:05:00"},
             {"name": "baird-10-01.png", "kind": "image", "modified": "2026-11-04T10:01:00"},
             {"name": "clip.mp4", "kind": "video", "modified": "2026-11-04T10:03:00"},
             {"name": "old.png", "kind": "image", "modified": "2026-11-03T09:00:00"}]

    def hub(app, tool, args):
        calls.append((app, tool, args))
        if app == "baird":
            return {"ok": True, "result": {"items": media}}
        steps = [_step(i + 1, "observe", e["app"], "", title=f"Ver {e['ref']}") for i, e in enumerate(args["events"])]
        return {"ok": True, "result": {"schema": sr.SCHEMA, "title": args.get("title"), "steps": steps, "inputs": [], "unknowns": []}}

    monkeypatch.setattr(sr, "_hub_call", hub)
    got = sr.baird_events(start="2026-11-04T10:00", end="2026-11-04T11:00", now=dt(2026, 11, 4, 12))
    assert [e["ref"] for e in got["events"]] == ["baird-10-01.png", "baird-10-05.png"] and got["clips_in_range"] == 1
    assert all(e["type"] == "screenshot" and e["app"] == sr.BAIRD_APP for e in got["events"])
    out = derivation_ops.run_recipe(OWNER, {"action": "screen_draft", "source": "baird", "start": "2026-11-04T10:00",
                                            "end": "2026-11-04T11:00", "title": "Alta en el otro equipo"})
    assert calls[-1][:2] == ("talos", "activity_procedure_draft") and len(calls[-1][2]["events"]) == 2
    assert out["draft"]["observed_with"] == "baird" and len(out["summary"]["steps"]) == 2
    codes = [u["code"] for u in out["draft"]["unknowns"]]
    assert "hdmi_pictures_only" in codes and "clips_not_segmented" in codes
    assert {r["action"] for r in out["suggestions"]["suggestions"]} == {"skip"}   # pictures alone suggest no tool
    made = derivation_ops.run_recipe(OWNER, {"action": "from_screen", "draft": out["draft"],
                                             "mapping": [{"step": 1, "tool": "write_file", "args": {"path": "alta.txt", "content": "alta"}}]})
    assert made["recipe"]["source"]["observed_with"] == "baird" and "HDMI" in " ".join(made["recipe"]["limits"])
    empty = derivation_ops.run_recipe(OWNER, {"action": "screen_draft", "source": "baird", "start": "2026-11-01T10:00", "end": "2026-11-01T11:00"})
    assert empty["error_class"] == "screen.baird_empty"
    monkeypatch.setattr(sr, "_hub_call", lambda app, tool, args: {"ok": True, "result": {"items": media}} if app == "baird"
                        else {"ok": False, "error": "module_disabled"})
    off = derivation_ops.run_recipe(OWNER, {"action": "screen_draft", "source": "baird", "start": "2026-11-04T10:00", "end": "2026-11-04T11:00"})
    assert off["error_class"] == "screen.talos_unavailable" and len(off["events"]) == 2 and "events" in off["error"]
