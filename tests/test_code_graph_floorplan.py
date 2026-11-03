"""Tests for `code_graph.floorplan` — the plain-language workspace map.

Deterministic parts only: route/request extraction, the per-file scan, the
label cache keyed by content hash (a fake labeller counts what is sent to the
"model"), the self-contained HTML export and the blueprint matcher. A tiny
fixture repo is indexed through the real code graph, the same way the
communities tests do it.
"""
from __future__ import annotations

import importlib
import json
import os
import re

import pytest

from src import tool_execution as te
from src.context_engine import store as ce_store

fp = importlib.import_module("src.code_graph.floorplan")

ROUTES_PY = '''"""Order endpoints for the shop."""
from fastapi import APIRouter

from services.orders import place_order, list_orders

router = APIRouter(prefix="/api")


@router.get("/orders")
def get_orders():
    return list_orders()


@router.post("/orders")
def create_order(payload):
    return place_order(payload)
'''

ORDERS_PY = '''"""Order business logic: placing and listing orders."""
import requests


def place_order(payload):
    requests.post("https://pay.example.com/charge", json=payload)
    return {"ok": True}


def list_orders():
    return []
'''

APP_JS = '''// Shop front end: shows the order list and sends new orders.
import axios from "axios";

export async function loadOrders() {
  const res = await fetch("/api/orders");
  return res.json();
}

export function sendOrder(order) {
  return fetch("/api/orders", { method: "POST", body: JSON.stringify(order) });
}

export const refresh = () => axios.get("/api/orders");
'''

TEST_PY = '''from services.orders import list_orders


def test_list_orders():
    assert list_orders() == []
'''


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = tmp_path / "shop"
    root.mkdir()
    _write(str(root), "web/routes.py", ROUTES_PY)
    _write(str(root), "services/orders.py", ORDERS_PY)
    _write(str(root), "frontend/app.js", APP_JS)
    _write(str(root), "tests/test_orders.py", TEST_PY)
    ce_store.use_path(str(tmp_path / "ce.db"))
    monkeypatch.setattr(fp, "CACHE_DIR", str(tmp_path / "floorplan-cache"))
    ws = te._active_workspace.set(str(root))
    roots = te._active_workspace_roots.set((str(root),))
    try:
        yield str(root)
    finally:
        te._active_workspace.reset(ws)
        te._active_workspace_roots.reset(roots)
        ce_store.use_path(None)


class FakeLabeler:
    """Answers every `key: X` in the prompt and records which keys it saw."""

    def __init__(self):
        self.seen = []

    def __call__(self, prompt):
        keys = re.findall(r"^key: (.+)$", prompt, re.M)
        self.seen.extend(keys)
        return json.dumps({k: f"plain words about {k}" for k in keys})


def _files(plan):
    return {f["path"]: f for p in plan["parts"] for f in p["files"]}


def test_python_routes_apply_the_router_prefix():
    routes = fp.extract_routes(ROUTES_PY, "python")
    assert [(r["method"], r["path"]) for r in routes] == [("GET", "/api/orders"), ("POST", "/api/orders")]
    assert routes[0]["line"] == 9


def test_flask_style_route_methods_and_js_server_routes():
    flask = 'bp = Blueprint("x", __name__, url_prefix="/v1")\n@bp.route("/items", methods=["GET", "POST"])\ndef items():\n    pass\n'
    assert fp.extract_routes(flask, "python") == [{"method": "GET/POST", "path": "/v1/items", "line": 2}]
    js = "const app = express();\napp.get('/health', ok);\nrouter.delete(\"/items/:id\", del);\n"
    assert [(r["method"], r["path"]) for r in fp.extract_routes(js, "javascript")] == [
        ("GET", "/health"), ("DELETE", "/items/:id")]


def test_outgoing_requests_in_js_and_python():
    js = fp.extract_requests(APP_JS, "javascript")
    assert [(r["method"], r["url"]) for r in js] == [
        ("GET", "/api/orders"), ("POST", "/api/orders"), ("GET", "/api/orders")]
    py = fp.extract_requests(ORDERS_PY, "python")
    assert [(r["method"], r["url"]) for r in py] == [("POST", "https://pay.example.com/charge")]


def test_scan_file_reads_docstring_symbols_and_hash(repo):
    info = fp.scan_file(repo, "services/orders.py")
    assert info["language"] == "python"
    assert info["symbols"] == ["place_order", "list_orders"]
    assert info["doc"] == "Order business logic: placing and listing orders."
    assert len(info["hash"]) == 40
    js = fp.scan_file(repo, "frontend/app.js")
    assert js["doc"].startswith("Shop front end")
    assert {"loadOrders", "sendOrder", "refresh"} <= set(js["symbols"])
    assert fp.scan_file(repo, "README.md") is None


def test_generate_without_a_model_uses_deterministic_labels(repo):
    plan = fp.generate(repo, labels="off")["plan"]
    files = _files(plan)
    assert set(files) == {"web/routes.py", "services/orders.py", "frontend/app.js"}
    assert files["services/orders.py"]["label"] == "Order business logic: placing and listing orders."
    assert files["services/orders.py"]["label_source"] == "docstring"
    assert plan["stats"]["routes"] == 2 and plan["stats"]["requests"] == 4
    assert plan["stats"]["labelled_now"] == 0
    assert all(p["name"] and p["label"] for p in plan["parts"])
    assert fp.open_plan(repo)["plan"]["generated_at"] == plan["generated_at"]


def test_labels_are_cached_by_hash_and_only_changed_files_are_relabelled(repo):
    first = FakeLabeler()
    plan = fp.generate(repo, labeler=first)["plan"]
    assert {"web/routes.py", "services/orders.py", "frontend/app.js"} <= set(first.seen)
    assert plan["stats"]["labelled_now"] == 3
    assert _files(plan)["web/routes.py"]["label"] == "plain words about web/routes.py"
    assert all(p["label_source"] == "model" for p in plan["parts"])

    second = FakeLabeler()
    plan = fp.generate(repo, labeler=second)["plan"]
    assert second.seen == []
    assert plan["stats"]["reused_labels"] == 3 and plan["stats"]["labelled_now"] == 0

    _write(repo, "services/orders.py", ORDERS_PY + "\n\ndef cancel_order(order_id):\n    return order_id\n")
    third = FakeLabeler()
    plan = fp.generate(repo, labeler=third)["plan"]
    file_keys = [k for k in third.seen if "/" in k]
    assert file_keys == ["services/orders.py"]
    assert plan["stats"]["labelled_now"] == 1 and plan["stats"]["reused_labels"] == 2


def test_label_budget_leaves_the_rest_for_the_next_run(repo):
    lab = FakeLabeler()
    plan = fp.generate(repo, labeler=lab, max_labels=1)["plan"]
    assert plan["stats"]["labelled_now"] == 1 and plan["stats"]["pending_labels"] == 2
    lab2 = FakeLabeler()
    plan = fp.generate(repo, labeler=lab2, max_labels=10)["plan"]
    assert len([k for k in lab2.seen if "/" in k]) == 2
    assert plan["stats"]["pending_labels"] == 0


def test_html_export_is_self_contained_and_escapes_the_data(repo):
    plan = fp.generate(repo, labels="off")["plan"]
    plan["parts"][0]["label"] = "</script><script>alert(1)</script>"
    html_text = fp.export_html(plan, studio_base="http://127.0.0.1:7000")
    assert "<script src" not in html_text and "<link" not in html_text
    assert html_text.count("</script>") == 2
    raw = html_text.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    data = json.loads(raw)
    assert data["root"] == plan["root"]
    assert '"http://127.0.0.1:7000"' in html_text and "/studio?workspace=" in html_text
    out = fp.export(repo)
    assert out["exit_code"] == 0 and os.path.isfile(out["path"])
    assert not out["path"].startswith(repo)


def test_blueprint_marks_existing_and_missing_parts(repo):
    fp.generate(repo, labels="off")
    goal = "# Shop\n- Orders API: list and place orders\n- Email notifications to customers\n"
    res = fp.blueprint(repo, goal, labels="off")
    marked = {m["name"]: m for m in res["blueprint"]["parts"]}
    assert marked["Orders API"]["status"] == "exists"
    assert marked["Orders API"]["matches"][0]["files"]
    assert marked["Email notifications to customers"]["status"] == "missing"
    assert fp.open_plan(repo)["plan"]["blueprint"]["summary"]["missing"] >= 1


def test_blueprint_uses_the_model_list_when_given(repo):
    fp.generate(repo, labels="off")

    def model(prompt):
        return json.dumps({"parts": [{"name": "Checkout", "description": "pay for an order",
                                      "keywords": ["order", "charge", "place"]},
                                     {"name": "Wishlist", "description": "saved items",
                                      "keywords": ["wishlist", "favourite"]}]})

    res = fp.blueprint(repo, "Users check out and keep a wishlist.", labeler=model)
    bp = res["blueprint"]
    assert bp["source"] == "model"
    status = {m["name"]: m["status"] for m in bp["parts"]}
    assert status["Checkout"] in ("exists", "partial") and status["Wishlist"] == "missing"


def test_blueprint_requires_goal_text(repo):
    assert fp.blueprint(repo, "  ")["exit_code"] == 1


def test_blueprint_model_judge_can_correct_a_keyword_match(repo):
    fp.generate(repo, labels="off")

    def model(prompt):
        if "PLANNED part" in prompt:
            return json.dumps({"P0": {"status": "partial", "part": "", "why": "orders exist, no refunds"},
                               "P1": {"status": "missing", "part": "", "why": "nothing sends mail"}})
        return json.dumps({"parts": [{"name": "Orders and refunds", "description": "orders",
                                      "keywords": ["order", "refund"]},
                                     {"name": "Mailer", "description": "e-mail", "keywords": ["mail"]}]})

    bp = fp.blueprint(repo, "Orders with refunds; a mailer.", labeler=model)["blueprint"]
    assert bp["source"] == "model+model-judge"
    first, second = bp["parts"]
    assert first["status"] == "partial" and first["judged_by"] == "model"
    assert first["why"] == "orders exist, no refunds"
    assert second["status"] == "missing"


def test_generated_and_minified_files_are_left_out(repo):
    _write(repo, "dist/assets/index-abc.js", "fetch('/api/orders');\n")
    _write(repo, "frontend/vendor.min.js", "fetch('/x');\n")
    files = _files(fp.generate(repo, labels="off")["plan"])
    assert "dist/assets/index-abc.js" not in files and "frontend/vendor.min.js" not in files


def test_served_export_marks_its_inline_scripts_with_the_nonce():
    page = fp.export_html({"root": "/r", "parts": [], "stats": {}}, nonce="n0nce")
    assert page.count('nonce="n0nce"') == 2
    assert "nonce" not in fp.export_html({"root": "/r", "parts": [], "stats": {}})
