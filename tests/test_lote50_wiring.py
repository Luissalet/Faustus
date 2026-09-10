"""Lote 50 — integración de la ola 7 (lotes 45-49b): pruebas de que cada
cableado descrito en `docs/spec/v2/MAPA_P1.md` § "Lote 50" está realmente
enchufado, no solo importado sin usar. Cada test de esta lista falla si se
deshace el cableado correspondiente (revert-check manual documentado en el
informe final de este lote).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── (45) routes/code_index_routes.py reindex → refresh_async ───────────────

def test_reindex_route_calls_refresh_async_not_the_blocking_refresh(monkeypatch):
    import routes.code_index_routes as code_index_routes
    from routes.code_index_routes import setup_code_index_routes
    from src import code_index

    monkeypatch.setattr(code_index_routes, "require_admin", lambda request: None)

    async def fake_refresh_async(workspace, *, project_id="", full=False):
        return {"ok": True, "reindexed": 0, "async": True}

    def refresh_should_not_be_called(*_args, **_kwargs):
        raise AssertionError("reindex route must await refresh_async, not call refresh() inline")

    monkeypatch.setattr(code_index, "refresh_async", fake_refresh_async)
    monkeypatch.setattr(code_index, "refresh", refresh_should_not_be_called)

    app = FastAPI()
    app.include_router(setup_code_index_routes())
    client = TestClient(app)
    resp = client.post("/api/code-index/proj1/reindex", json={"path": "/tmp"})
    assert resp.status_code == 200
    assert resp.json()["refresh"]["async"] is True


# ── (46) TOOL-06 gate wired into routes/skills_routes.py ───────────────────

def test_update_skill_route_refuses_a_mutating_update_on_an_obsolete_skill(monkeypatch):
    from core.middleware import auth_disabled
    import routes.skills_routes as skills_routes

    class FakeManager:
        def load(self, owner=None):
            return [{"name": "old-skill", "status": "deprecated", "owner": None, "source": "learned"}]

        def update_skill(self, *_args, **_kwargs):
            raise AssertionError("update_skill must not be reached — the gate must refuse first")

    monkeypatch.setattr(auth_disabled, "__call__", lambda: True, raising=False)
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        request.state.current_user = None
        return await call_next(request)

    app.include_router(skills_routes.setup_skills_routes(FakeManager()))
    client = TestClient(app)
    resp = client.put("/api/skills/old-skill", json={"description": "new text"})
    assert resp.status_code == 409


def test_update_skill_route_refuses_an_unreviewed_promotion_from_teach_mode(monkeypatch):
    from core.middleware import auth_disabled
    import routes.skills_routes as skills_routes

    class FakeManager:
        def load(self, owner=None):
            return [{"name": "s1", "status": "draft", "owner": None, "source": "teach_mode"}]

        def update_skill(self, *_args, **_kwargs):
            raise AssertionError("update_skill must not be reached without reviewed=True")

    monkeypatch.setattr(auth_disabled, "__call__", lambda: True, raising=False)
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        request.state.current_user = None
        return await call_next(request)

    app.include_router(skills_routes.setup_skills_routes(FakeManager()))
    client = TestClient(app)
    resp = client.put("/api/skills/s1", json={"status": "published"})
    assert resp.status_code == 409


# ── (46) TOOL-06 gate wired into teach_mode install transition ─────────────

def test_teach_mode_install_calls_validate_promotion(monkeypatch, tmp_path):
    from src.durable_feature_store import DurableFeatureStore
    from src.teach_mode.service import TeachService
    from src import skill_governance
    import src.constants as constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    teach = TeachService(DurableFeatureStore(str(tmp_path / "teach.db")))
    monkeypatch.setattr(teach, "_register_with_immune", lambda *_a, **_kw: None)

    calls = []
    real = skill_governance.validate_promotion

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(skill_governance, "validate_promotion", spy)

    demo = teach.start(owner="alice", request={"title": "T", "intent": "Do T"})
    teach.observe(owner="alice", demonstration_id=demo["id"],
                  observation={"tool": "read_file", "arguments": {}, "result": {}})
    teach.stop(owner="alice", demonstration_id=demo["id"])
    procedure = teach.compile(owner="alice", demonstration_id=demo["id"])
    procedure = teach.transition(owner="alice", procedure_id=procedure["id"], action="simulate")
    procedure = teach.transition(owner="alice", procedure_id=procedure["id"], action="validate",
                                 evidence={"passed": True, "proof_refs": ["proof:1"]})
    procedure = teach.transition(owner="alice", procedure_id=procedure["id"], action="approve")
    teach.transition(owner="alice", procedure_id=procedure["id"], action="install")
    assert calls, "install must call skill_governance.validate_promotion"
    assert calls[0][1]["reviewed"] is True


# ── (46) VER-03 available_models wired into agent_loop's resolve_reviewer ──

def test_available_models_for_review_reads_only_enabled_owned_endpoints(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database as db_mod

    url = "sqlite:///" + (tmp_path / "t.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)

    from src import auto_review
    with db_mod.SessionLocal() as db:
        db.add(db_mod.ModelEndpoint(id="e1", name="e1", base_url="http://x", is_enabled=True,
                                    owner="alice", cached_models=json.dumps(["model-a", "model-b"])))
        db.add(db_mod.ModelEndpoint(id="e2", name="e2", base_url="http://y", is_enabled=True,
                                    owner="bob", cached_models=json.dumps(["model-c"])))
        db.add(db_mod.ModelEndpoint(id="e3", name="e3", base_url="http://z", is_enabled=False,
                                    owner="alice", cached_models=json.dumps(["model-disabled"])))
        db.commit()

    ids = auto_review.available_models_for_review("alice")
    assert ids == ["model-a", "model-b"]

    # And resolve_reviewer actually picks a DIFFERENT model when one is available.
    reviewer = auto_review.resolve_reviewer("model-a", "same", available_models=ids)
    assert reviewer == "model-b"


# ── (46) PLAN-04 evidence_weighted_support wired into synthesis.build ──────

def test_synthesis_build_attaches_evidence_support_per_decision():
    from src.council import synthesis
    from src.council.contracts import CouncilDecision, CouncilMessage

    dec = CouncilDecision.parse({
        "session_id": "s1", "question": "which fix?", "status": "decided",
        "chosen": "patch A", "supporters": ["m1", "m2"],
    })

    class FakeLedger:
        session_id = "s1"

        def snapshot(self):
            return {
                "session_id": "s1",
                "decisions": [dec.to_mapping() if hasattr(dec, "to_mapping") else dict(dec.__dict__)],
                "tasks": [], "open_objections": [], "blocked_task_ids": [],
            }

    messages = [
        CouncilMessage.parse({"session_id": "s1", "author_id": "m1", "decision_id": dec.id,
                              "content": "agree", "metadata": {"evidence_refs": ["run#1"]}}),
        CouncilMessage.parse({"session_id": "s1", "author_id": "m2", "decision_id": dec.id,
                              "content": "agree", "metadata": {}}),
    ]
    summary = synthesis.build(FakeLedger(), messages=messages, participants=[], stop_reason="user_stopped")
    assert summary.decisions, "no decisions in the built summary"
    support = summary.decisions[0]["evidence_support"]
    assert support["with_evidence"] == 1
    assert support["unanimous_without_evidence"] is False


# ── (48) LANG-02 diacritic-folded fallback wired into searxng_search_api ───

def test_searxng_search_api_retries_with_diacritic_folded_variant_as_last_resort(monkeypatch):
    from services.search import providers

    monkeypatch.setattr(providers, "_get_search_instance", lambda: "http://searx.local")
    monkeypatch.setattr(providers, "_get_result_count", lambda: 10)
    monkeypatch.setattr(providers, "_safesearch_for", lambda _p: "1")
    monkeypatch.setattr(providers, "_GENERAL_ENGINES", "")
    monkeypatch.setattr(providers, "_record_engine_health", lambda *a, **k: None)

    calls = []

    class FakeResp:
        def __init__(self, q):
            self._q = q
        def raise_for_status(self):
            return None
        def json(self):
            calls.append(self._q)
            if self._q == "cafe":  # only the folded variant "succeeds"
                return {"results": [{"title": "t", "url": "http://x", "content": "c"}]}
            return {"results": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResp(params.get("q"))

    monkeypatch.setattr(providers.httpx, "get", fake_get)

    results = providers.searxng_search_api("café", count=5)
    assert results, "expected the diacritic-folded retry to find a result"
    assert "café" in calls and "cafe" in calls
    # `language`/`engines` pins must never be touched by the fallback.
    assert calls[0] == "café"


# ── (49b) app.py: startup warning on a non-loopback bind without TLS ───────

def test_insecure_bind_warning_fires_only_without_loopback_or_tls():
    import app as app_module

    assert app_module._insecure_bind_warning("127.0.0.1", None, None) is None
    assert app_module._insecure_bind_warning("0.0.0.0", None, None) is not None
    assert app_module._insecure_bind_warning("0.0.0.0", "cert.pem", "key.pem") is None


# ── (49b) tests/qa/test_qa_32_preview_malicioso.py covers Preview.tsx ──────

def test_qa_32_parametrize_list_includes_the_canvas_preview():
    qa32 = (REPO_ROOT / "tests" / "qa" / "test_qa_32_preview_malicioso.py").read_text(encoding="utf-8")
    assert "studio/src/components/Preview.tsx" in qa32


# ── (49b) BENCH-03: chat.ts → model.ts → Transcript.tsx → Studio.tsx ───────

def test_evidence_refs_wired_from_wire_to_tool_card_button():
    chat_ts = (REPO_ROOT / "studio/src/adapters/chat.ts").read_text(encoding="utf-8")
    model_ts = (REPO_ROOT / "studio/src/screens/studio/model.ts").read_text(encoding="utf-8")
    transcript_tsx = (REPO_ROOT / "studio/src/screens/studio/Transcript.tsx").read_text(encoding="utf-8")
    studio_tsx = (REPO_ROOT / "studio/src/screens/Studio.tsx").read_text(encoding="utf-8")

    assert "evidenceRefsFrom(raw.evidence_refs)" in chat_ts
    assert "evidenceRefs?: EvidenceRef[]" in model_ts
    assert "onOpenEvidence" in transcript_tsx and "step.evidenceRefs" in transcript_tsx
    assert "onOpenEvidence={setEvidenceRef}" in studio_tsx
    assert "EvidenceInspector" in studio_tsx
