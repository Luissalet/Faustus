import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from services.projects import ProjectStore, ProjectError, ExecutionDefaultsConflict
from src.project_execution_defaults import validate, patch, merge

DEFAULTS = {"autonomy_preset": "bounded_autonomous", "reasoning_effort": "high", "thinking_enabled": True, "initial_response_timeout_s": 300}


@pytest.mark.parametrize("changes", [
    {"thinking_enabled": "false"}, {"thinking_enabled": 1}, {"thinking_enabled": []},
    {"initial_response_timeout_s": True}, {"initial_response_timeout_s": "300"},
    {"initial_response_timeout_s": float('nan')}, {"initial_response_timeout_s": float('inf')},
    {"initial_response_timeout_s": 4}, {"initial_response_timeout_s": 901},
    {"autonomy_preset": "unrestricted"}, {"reasoning_effort": "invented"}, {"model": "new-model"},
    [], None,
])
def test_saved_preferences_are_strict(changes):
    with pytest.raises(ValueError): validate(changes, allow_clear=True)


def test_individual_clear_false_and_explicit_auto():
    saved = patch(DEFAULTS, {"reasoning_effort": None, "thinking_enabled": False})
    assert "reasoning_effort" not in saved and saved["thinking_enabled"] is False
    assert saved["initial_response_timeout_s"] == 300
    effective = merge(DEFAULTS, {"thinking_enabled": False, "reasoning_effort": "auto"})
    assert effective["thinking_enabled"] is False and effective["reasoning_effort"] == "auto"


def test_existing_explicit_mode_and_generation_override_remain_stronger():
    assert "thinking_enabled" not in merge(DEFAULTS, {"think_mode": "fast"})
    assert "reasoning_effort" not in merge(DEFAULTS, {"gen_overrides": json.dumps({"reasoning_effort": "low"})})
    assert "thinking_enabled" not in merge(DEFAULTS, {"gen_overrides": {"think": False}})
    assert "reasoning_effort" not in merge(DEFAULTS, {"gen_overrides": {"think": False}})
    assert merge(DEFAULTS, {}) == DEFAULTS
    assert merge({}, {}) == {}


@pytest.fixture
def store(tmp_path):
    return ProjectStore(str(tmp_path/'data'))


def test_owner_persistence_cas_and_unrelated_project_fields(store):
    project = store.create('Project', owner='alice', scaffold_memory=False)
    update = {"execution_defaults": DEFAULTS, "execution_defaults_expected_revision": 0}
    assert store.update(project['id'], update, 'bob') is None
    saved = store.update(project['id'], update, 'alice')
    assert saved['execution_defaults'] == DEFAULTS
    assert saved['execution_defaults_revision'] == 1 and saved['context_revision'] == 0
    assert ProjectStore(str(Path(store.path).parent)).get(project['id'], 'alice')['execution_defaults'] == DEFAULTS
    before = store.get(project['id'], 'alice')
    with pytest.raises(ExecutionDefaultsConflict):store.update(project['id'], update, 'alice')
    assert store.get(project['id'], 'alice') == before
    cleared = store.update(project['id'], {"execution_defaults": {"initial_response_timeout_s": None}, "execution_defaults_expected_revision": 1}, 'alice')
    assert 'initial_response_timeout_s' not in cleared['execution_defaults']
    assert cleared['execution_defaults']['reasoning_effort'] == 'high'
    ordinary = store.update(project['id'], {'instructions':'Keep this instruction.'}, 'alice')
    assert ordinary['execution_defaults'] == cleared['execution_defaults']


def test_concurrent_settings_editor_cannot_overwrite_new_revision(store):
    project = store.create('Concurrent', owner='alice', scaffold_memory=False)
    def save():
        try:return store.update(project['id'], {"execution_defaults": DEFAULTS, "execution_defaults_expected_revision": 0}, 'alice')
        except ExecutionDefaultsConflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:save(),range(2)))
    assert sum(value is not None for value in results)==1


def test_invalid_saved_payload_does_not_mutate_store(store):
    project=store.create('Strict',owner='alice',scaffold_memory=False)
    with pytest.raises(ProjectError):store.update(project['id'], {"execution_defaults":{"thinking_enabled":"false"},"execution_defaults_expected_revision":0},'alice')
    assert store.get(project['id'],'alice')==project


def test_request_budget_inherits_project_value_without_explicit_payload(monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from starlette.testclient import TestClient
    from src.first_token_budget import request_budget, effective
    monkeypatch.setattr('src.project_execution_defaults.for_session', lambda *a, **k:{'project_id':'project','defaults':DEFAULTS,'revision':7})
    app=FastAPI()
    @app.post('/turn')
    @request_budget
    async def turn(request: Request):
        async def stream():
            yield 'data: '+json.dumps({'wait':effective(30),'preferences':request.state.execution_defaults})+'\n\n'
        return StreamingResponse(stream(),media_type='text/event-stream')
    with TestClient(app) as client:
        inherited=client.post('/turn',json={'session':'own','message':'natural request'})
        assert inherited.headers['x-faustus-initial-response-timeout-s']=='300.0'
        assert inherited.headers['x-faustus-execution-defaults-revision']=='7'
        assert '"thinking_enabled": true' in inherited.text
        explicit=client.post('/turn',json={'session':'own','message':'natural request','initial_response_timeout_s':90,'thinking_enabled':False})
        assert '"wait": 90.0' in explicit.text and '"thinking_enabled": false' in explicit.text
    assert effective(30)==30


def test_studio_project_execution_adapters():
    import subprocess,shutil
    root=Path(__file__).resolve().parent.parent
    if not shutil.which('node') or not (root/'node_modules/esbuild').exists():pytest.skip('Node/esbuild unavailable')
    result=subprocess.run(['node','studio/checks/project-execution-defaults.check.mjs'],cwd=root,capture_output=True,text=True,encoding='utf8',timeout=60)
    assert result.returncode==0,result.stdout+result.stderr


def test_native_schema_describes_exact_fields_and_preserves_explicit_clear():
    from routes.project_routes import ExecutionDefaultsPatch
    from pydantic import ValidationError
    schema=ExecutionDefaultsPatch.model_json_schema()
    assert set(schema['properties'])==set(DEFAULTS)
    assert schema['additionalProperties'] is False
    assert ExecutionDefaultsPatch(reasoning_effort=None).model_dump(exclude_unset=True)=={'reasoning_effort':None}
    for values in ({'thinking_enabled':'false'},{'initial_response_timeout_s':True},{'initial_response_timeout_s':'300'},{'model':'invented'}):
        with pytest.raises(ValidationError):ExecutionDefaultsPatch(**values)
