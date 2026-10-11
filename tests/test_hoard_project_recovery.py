"""Historical work resolves to stable native IDs without reviving deleted data."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, Session, ChatMessage
from services.hoard_copilot import CopilotError, HoardCopilotStore
from services.hoard_project_recovery import HoardProjectRecovery
from services.projects import ProjectStore


@pytest.fixture
def recovery(tmp_path):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    store = HoardCopilotStore(str(tmp_path / 'links.db'), ProjectStore(str(tmp_path / 'projects.json')))
    service = HoardProjectRecovery(store, factory)
    def add(app='cicero', space='personal', external='deck-one', title='Presentación del puerto', owner='alice', text='Decidimos usar mapas del puerto.', when=datetime(2026,10,8,12), suffix=''):
        context = {'origin':{'app_id':app,'data_space_id':space},'project':{'external_project_id':external,'title':title},'selection':{'text':'selection that must not be recalled'}}
        linked = store.resolve_project(context, owner)
        sid = f'{app}-{space}-{external}-{owner}{suffix}'
        store.attach(sid, context, owner, linked['project_link_id'])
        with factory() as db:
            db.add(Session(id=sid,name=title,endpoint_url='http://unused',model='unused',owner=owner,project_id=linked['project_id']))
            if text is not None:
                db.add(ChatMessage(id='msg-'+sid,session_id=sid,role='user',content=text,timestamp=when))
            db.commit()
        return linked, sid
    yield service, add, factory
    engine.dispose()


def test_natural_words_and_accents_find_prior_conversation_with_evidence(recovery):
    service, add, _ = recovery
    linked, sid = add(title='Trabajo de octubre', text='Elegimos la canción del puerto para cerrar el vídeo.')
    add(app='writer',title='Novela de montaña',text='Capítulo sobre la cordillera.')
    result = service.search('alice','Ese proyecto de la canción del puerto que hicimos el otro día')
    assert result['total'] == 1
    found = result['projects'][0]
    assert found['id'] == linked['project_link_id']
    assert found['external_project_id'] == 'deck-one'
    assert found['evidence'][0]['session_id'] == sid
    assert 'canción del puerto' in found['evidence'][0]['excerpt']


def test_date_filters_use_work_not_empty_panel_creation(recovery):
    service, add, _ = recovery
    add(external='old',when=datetime(2026,10,8,12))
    add(external='empty',title='Panel abierto hoy',text=None)
    add(external='yesterday',when=datetime(2026,10,10,17))
    result = service.search('alice', after='2026-10-10T00:00:00+02:00', before='2026-10-11T00:00:00+02:00')
    assert [row['external_project_id'] for row in result['projects']] == ['yesterday']


def test_same_titles_remain_distinct_across_apps_spaces_and_owners(recovery):
    service, add, _ = recovery
    first, _ = add()
    second, _ = add(space='work')
    add(app='writer')
    hidden, _ = add(owner='bob')
    rows = service.search('alice',query='puerto',app_id='cicero')['projects']
    assert {row['id'] for row in rows} == {first['project_link_id'],second['project_link_id']}
    assert len(service.search('alice', data_space_id='work')['projects']) == 1
    with pytest.raises(CopilotError): service.recall('alice',hidden['project_link_id'])


def test_deleted_session_does_not_supply_historical_evidence(recovery):
    service, add, factory = recovery
    linked, sid = add(title='October',text='A distinctive abandoned plan')
    with factory() as db:
        db.query(ChatMessage).filter(ChatMessage.session_id==sid).delete()
        db.query(Session).filter(Session.id==sid).delete()
        db.commit()
    assert service.search('alice','abandoned')['total'] == 0
    assert service.recall('alice',linked['project_link_id'])['conversations'] == []


def test_recall_keeps_native_identity_memory_and_optional_workspace_without_selection(recovery, tmp_path):
    service, add, _ = recovery
    linked, sid = add()
    recalled = service.recall('alice',linked['project_link_id'])
    assert recalled['memory_scope']['project_id'] == linked['project_id']
    assert recalled['context']['project']['external_project_id'] == 'deck-one'
    assert 'selection' not in recalled['context']
    assert recalled['workspace'] is None
    assert not (tmp_path / '.git').exists()
    assert recalled['conversations'][0]['session_id'] == sid


def test_evidence_excerpt_contains_a_match_far_into_long_text_and_paginates(recovery):
    service, add, _ = recovery
    add(external='one',title='A',text='inicio ' * 500 + 'aeronáutica elegida')
    add(external='two',title='B',text='otra aeronáutica')
    result = service.search('alice','aeronautica',limit=1)
    assert result['total'] == 2 and result['next_offset'] == 1
    assert 'aeronáutica' in result['projects'][0]['evidence'][0]['excerpt']
    assert service.search('alice','aeronautica',limit=1,offset=1)['projects'][0]['id'] != result['projects'][0]['id']


def test_unicode_names_are_searchable_without_becoming_an_empty_query(recovery):
    service, add, _ = recovery
    add(title='Порт', text='Карта гавани')
    add(external='other', title='Mountain', text='An unrelated manuscript')
    result = service.search('alice','Порт')
    assert result['total'] == 1 and result['projects'][0]['title'] == 'Порт'


def test_deleted_working_project_is_reported_without_recreating_it(recovery):
    service, add, _ = recovery
    linked, _ = add()
    assert service.store.project_store().delete(linked['project_id'], 'alice')
    recalled = service.recall('alice',linked['project_link_id'])
    assert recalled['available'] is False
    assert not service.store.project_store().get(linked['project_id'], 'alice')


def test_recent_work_is_ordered_by_message_date_across_chats(recovery):
    service, add, _ = recovery
    linked, _ = add(text='Recent decision',when=datetime(2026,10,10),suffix='new')
    add(text='Older decision inserted later',when=datetime(2026,10,8),suffix='old')
    recalled = service.recall('alice',linked['project_link_id'])
    assert recalled['recent_messages'][-1]['text'] == 'Recent decision'


def test_native_no_login_tool_owner_uses_the_same_bucket_as_api(recovery, monkeypatch):
    from src.owner_identity import effective_storage_owner
    monkeypatch.setenv('AUTH_ENABLED', 'false')
    service, add, _ = recovery
    linked, _ = add(owner=effective_storage_owner(None))
    add(owner='bob',title='Other owner',text='Private unrelated work')
    found = service.search(None,'puerto')
    assert found['total'] == 1 and found['projects'][0]['id'] == linked['project_link_id']
    assert service.recall(None,linked['project_link_id'])['memory_scope']['project_id'] == linked['project_id']
