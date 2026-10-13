import json
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from httpx import ASGITransport,AsyncClient
from core import database as db
from routes import model_routes

@pytest.fixture
def defaults_api(monkeypatch):
    engine=create_engine('sqlite:///:memory:',connect_args={'check_same_thread':False},poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    local=sessionmaker(bind=engine)
    with local() as session:
        session.add_all([db.ModelEndpoint(id='sparks-selected',name='Sparks global',base_url='http://private-spark/v1',is_enabled=False,cached_models=json.dumps(['global-model']),owner=None),
                        db.ModelEndpoint(id='pc-other',name='Historical PC',base_url='http://private-pc/v1',is_enabled=True,cached_models=json.dumps(['historical-model']),owner=None)])
        session.commit()
    monkeypatch.setenv('AUTH_ENABLED','false')
    monkeypatch.setattr(model_routes,'SessionLocal',local)
    monkeypatch.setattr(model_routes,'ModelEndpoint',db.ModelEndpoint)
    settings={'default_endpoint_id':'sparks-selected','default_model':'global-model'}
    monkeypatch.setattr(model_routes,'_load_settings',lambda:settings)
    app=FastAPI();app.include_router(model_routes.setup_model_routes(None))
    yield app,local,settings
    engine.dispose()

async def test_disabled_default_retains_its_model_and_endpoint_not_first_pc(defaults_api):
    app,local,settings=defaults_api
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        result=(await client.get('/api/default-chat')).json()
        assert result['endpoint_id']=='sparks-selected' and result['model']=='global-model'
        assert result['missing'] and 'private-spark' in result['endpoint_url']
        with local() as session:
            row=session.get(db.ModelEndpoint,'sparks-selected');row.is_enabled=True;session.commit()
        healthy=(await client.get('/api/default-chat')).json()
        assert healthy['endpoint_id']=='sparks-selected' and not healthy.get('missing')
        assert settings['default_endpoint_id']=='sparks-selected'

async def test_removed_configured_default_is_not_replaced_with_pc(defaults_api):
    app,_,settings=defaults_api;settings['default_endpoint_id']='removed-spark'
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        result=(await client.get('/api/default-chat')).json()
        assert result['endpoint_id']=='removed-spark' and result['model']=='global-model'
        assert result['missing'] and result['endpoint_url']==''

async def test_empty_model_on_disabled_default_uses_its_own_cached_model(defaults_api):
    app,_,settings=defaults_api;settings['default_model']=''
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
        result=(await client.get('/api/default-chat')).json()
        assert result['model']=='global-model' and result['endpoint_id']=='sparks-selected'
