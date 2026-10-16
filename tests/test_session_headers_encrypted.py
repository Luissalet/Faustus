import json
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import Base, Session


def test_static_provider_headers_are_encrypted_and_survive_new_session(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'headers.db'))
    Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine)
    ident = uuid.uuid4().hex
    headers = {'Authorization': 'Bearer test-qwen-private-key', 'X-Label': 'revisión'}
    with make_session() as db:
        db.add(Session(id=ident, name='Reviewer', endpoint_url='http://spark4/v1/chat/completions', model='qwen', headers=headers))
        db.commit()
    with engine.connect() as connection:
        stored = connection.execute(text('SELECT headers FROM sessions WHERE id=:id'), {'id': ident}).scalar_one()
        assert 'test-qwen-private-key' not in stored
        assert json.loads(stored).startswith('enc:')
    with make_session() as fresh:
        assert fresh.get(Session, ident).headers == headers
    engine.dispose()


def test_legacy_plain_headers_stay_readable_until_their_own_next_write(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'legacy.db'))
    Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine)
    ident = uuid.uuid4().hex
    with make_session() as db:
        db.add(Session(id=ident, name='Legacy', endpoint_url='http://spark/v1/chat/completions', model='legacy', headers={}))
        db.commit()
    legacy = {'Authorization': 'Bearer legacy-test-key'}
    raw = json.dumps(legacy)
    with engine.begin() as connection:
        connection.execute(text('UPDATE sessions SET headers=:headers WHERE id=:id'), {'id': ident, 'headers': raw})
    with make_session() as db:
        row = db.get(Session, ident)
        assert row.headers == legacy
        row.name = 'Renamed'
        db.commit()
    with engine.connect() as connection:
        assert connection.execute(text('SELECT headers FROM sessions WHERE id=:id'), {'id': ident}).scalar_one() == raw
    with make_session() as db:
        db.get(Session, ident).headers = {**legacy, 'Accept': 'application/json'}
        db.commit()
    with engine.connect() as connection:
        stored = connection.execute(text('SELECT headers FROM sessions WHERE id=:id'), {'id': ident}).scalar_one()
        assert 'legacy-test-key' not in stored and json.loads(stored).startswith('enc:')
    engine.dispose()
