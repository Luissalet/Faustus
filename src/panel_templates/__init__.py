"""src/panel_templates -- ready-made panels: an HTML page (in this folder), the tables it needs and
its server actions. Creating one from a template is a normal panel afterwards (versions, check,
changes in a chat).

    gpu      GPU y modelos      live cards per GPU and loaded models, history sampled every 5 minutes
    agora    Ágora              tasks per space from the Hub, with state, owner and text filters
    darwin   Radar de Darwin    proposals by state and app, latest captures, trend sampled every 6 hours
    notas    Notas y tablero    quick notes and a kanban board in the panel's own database
    uso      Uso                conversations, turns, tokens per model, tools and busiest days
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from src import panel_actions, panel_db, panels

HERE = Path(__file__).resolve().parent

TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "gpu", "name": "GPU y modelos", "icon": "cpu",
        "description": "Cada GPU con su uso, memoria, temperatura y potencia, los modelos cargados y el histórico con una muestra cada 5 minutos",
        "source": "/api/system/usage",
        "setup_sql": (
            "CREATE TABLE IF NOT EXISTS muestras (id INTEGER PRIMARY KEY AUTOINCREMENT, ts, util, util_avg, mem_used, "
            "mem_total, power, temp, ram_pct, cpu_pct, modelos);\n"
            "CREATE INDEX IF NOT EXISTS muestras_ts ON muestras(ts);\n"
            "CREATE TABLE IF NOT EXISTS gpus (id INTEGER PRIMARY KEY AUTOINCREMENT, ts, idx, nombre, util, mem_used, "
            "mem_total, temp, power);\n"
            "CREATE INDEX IF NOT EXISTS gpus_ts ON gpus(ts);"
        ),
        "actions": [{
            "name": "muestra", "label": "Muestra de GPU y modelos", "every_minutes": 5, "url": "/api/system/usage",
            "save": [
                {"table": "muestras", "keep": 8640, "columns": {
                    "ts": "$now", "util": "gpu_pool.util", "util_avg": "gpu_pool.util_avg", "mem_used": "gpu_pool.mem_used",
                    "mem_total": "gpu_pool.mem_total", "power": "gpu_pool.power", "temp": "gpu_pool.temp",
                    "ram_pct": "ram.percent", "cpu_pct": "cpu.percent", "modelos": "resident.models[].model"}},
                {"table": "gpus", "rows": "gpu", "keep": 40000, "columns": {
                    "ts": "$now", "idx": "index", "nombre": "name", "util": "util", "mem_used": "mem_used",
                    "mem_total": "mem_total", "temp": "temp", "power": "power"}},
            ],
        }],
        "first_run": ["muestra"],
    },
    {
        "id": "agora", "name": "Ágora", "icon": "list-checks",
        "description": "Tareas del Ágora por espacio, con estado, responsable, búsqueda y detalle",
        "source": "faustus.hub('/api/agora/spaces'), faustus.hub('/api/agora/tasks')",
    },
    {
        "id": "darwin", "name": "Radar de Darwin", "icon": "sparkles",
        "description": "Propuestas de Darwin por estado y aplicación, últimas capturas y descubrimientos, y su evolución",
        "source": "darwin_overview, darwin_proposals, darwin_changes_since (Darwin's Hoard)",
        "setup_sql": (
            "CREATE TABLE IF NOT EXISTS resumen (id INTEGER PRIMARY KEY AUTOINCREMENT, ts, descubrimientos, propuestas, "
            "pendientes, aceptadas, cubiertas, descartadas, entregas, huecos);"
        ),
        "actions": [{
            "name": "resumen", "label": "Resumen del radar", "every_minutes": 360, "tool": "darwin_overview",
            "app": "darwin", "args": {},
            "save": {"table": "resumen", "keep": 2000, "columns": {
                "ts": "$now", "descubrimientos": "counts.sources", "propuestas": "counts.proposals",
                "pendientes": "proposals_by_state.pendiente", "aceptadas": "proposals_by_state.aceptada",
                "cubiertas": "proposals_by_state.cubierta", "descartadas": "proposals_by_state.descartada",
                "entregas": "counts.deliveries", "huecos": "counts.gaps_open"}},
        }],
        "first_run": ["resumen"],
    },
    {
        "id": "notas", "name": "Notas y tablero", "icon": "file-text",
        "description": "Notas rápidas y un tablero por columnas guardados en la base de datos del panel",
        "source": "la base de datos del panel",
        "setup_sql": (
            "CREATE TABLE IF NOT EXISTS tarjetas (id INTEGER PRIMARY KEY AUTOINCREMENT, titulo TEXT NOT NULL, "
            "texto TEXT NOT NULL DEFAULT '', columna TEXT NOT NULL DEFAULT 'ideas', orden REAL NOT NULL DEFAULT 0, "
            "color TEXT NOT NULL DEFAULT '', creado TEXT, actualizado TEXT);\n"
            "CREATE INDEX IF NOT EXISTS tarjetas_columna ON tarjetas(columna, orden);"
        ),
    },
    {
        "id": "uso", "name": "Uso", "icon": "bar-chart-3",
        "description": "Conversaciones, turnos, tokens por modelo, herramientas y días con más actividad",
        "source": "/api/usage/recap",
    },
]


def _by_id(template_id: Any) -> Dict[str, Any]:
    for t in TEMPLATES:
        if t["id"] == str(template_id or "").strip().lower():
            return t
    raise panels.PanelError(f"no template {template_id!r}; there are: {', '.join(t['id'] for t in TEMPLATES)}",
                            "panel.not_found")


def html(template_id: str) -> str:
    return (HERE / f"{_by_id(template_id)['id']}.html").read_text(encoding="utf-8")


def list_templates() -> List[Dict[str, Any]]:
    return [{"id": t["id"], "name": t["name"], "icon": t["icon"], "description": t["description"],
             "source": t.get("source", ""),
             "actions": [{"name": a["name"], "label": a.get("label"), "schedule": panel_actions.schedule_of(a)}
                         for a in t.get("actions", [])],
             "tables": bool(t.get("setup_sql"))} for t in TEMPLATES]


async def create_from_template(template_id: Any, *, name: Any = None, panel_id: Any = None,
                               owner: Optional[str] = None, first_run: bool = True) -> Dict[str, Any]:
    """Create a panel from a template: its page, its tables, its actions (scheduled), and one first
    run of the actions that fill its history so the page has something to show at once."""
    import asyncio
    t = _by_id(template_id)
    meta = await asyncio.to_thread(panels.create, name or t["name"], html(t["id"]), icon=t["icon"],
                                   description=t["description"], panel_id=panel_id or None)
    pid = meta["id"]
    if t.get("setup_sql"):
        await asyncio.to_thread(panel_db.exec_, pid, t["setup_sql"])
    actions = []
    for spec in t.get("actions", []):
        out = await asyncio.to_thread(panel_actions.set_action, pid, dict(spec), owner=owner)
        actions.append(out["action"]["name"])
    runs = []
    if first_run:
        for act in t.get("first_run", []):
            try:
                r = await panel_actions.run_action(pid, act, owner=owner, trigger="template")
                runs.append({"action": act, "ok": True, "saved": r["saved"]})
            except panels.PanelError as exc:
                runs.append({"action": act, "ok": False, "error": exc.detail})
    return {**meta, "template": t["id"], "actions": actions, "first_runs": runs}
