"""Native session creation binds owned enabled projects before a chat exists."""
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from services.projects import ProjectStore
from routes import session_routes as sr

@pytest.fixture
def api(tmp_path,monkeypatch):
 store=ProjectStore(str(tmp_path/'data'))
 monkeypatch.setattr('services.projects.get_store',lambda:store)
 monkeypatch.setattr(sr,'effective_user',lambda request:request.headers.get('x-owner','alice'))
 monkeypatch.setattr('src.event_bus.fire_event',lambda *args:None)
 calls=[]
 class Manager:
  def create_session(self,**kwargs):
   calls.append(kwargs);return SimpleNamespace(id=kwargs['session_id'],name=kwargs['name'],headers={})
 app=FastAPI();app.include_router(sr.setup_session_routes(Manager(),{}))
 with TestClient(app) as client:yield client,store,calls

def test_owned_project_is_bound_in_one_native_creation(api):
 client,store,calls=api;p=store.create('Owned',owner='alice',workspace='',scaffold_memory=False)
 response=client.post('/api/session',data={'name':'New','model':'qa','skip_validation':'true','project_id':p['id']})
 assert response.status_code==200,response.text
 assert calls[0]['project_id']==p['id'] and calls[0]['folder']==p['folder'] and calls[0]['owner']=='alice'

@pytest.mark.parametrize('kind',['foreign','missing','disabled'])
def test_unavailable_project_never_creates_a_session(api,kind):
 client,store,calls=api
 p=store.create('Other',owner='bob' if kind=='foreign' else 'alice',scaffold_memory=False)
 if kind=='disabled':store.update(p['id'],{'enabled':False},'alice')
 response=client.post('/api/session',data={'model':'qa','skip_validation':'true','project_id':'missing' if kind=='missing' else p['id']})
 assert response.status_code==(400 if kind=='disabled' else 404)
 assert calls==[]

def test_no_project_keeps_legacy_creation(api):
 client,store,calls=api
 response=client.post('/api/session',data={'model':'qa','skip_validation':'true'})
 assert response.status_code==200
 assert calls[0]['project_id'] is None and calls[0]['folder'] is None

