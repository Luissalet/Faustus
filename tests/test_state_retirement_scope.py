"""Discovery is authoritative only for its scope and only when healthy."""
import pytest

from src.state_mirror.adapters.base import Scope, ThreadedAdapter, entity, observation
from src.state_mirror.persistence import StateStore
from src.state_mirror.reconcile import sweep


class ScopedAdapter(ThreadedAdapter):
    name = 'scoped-test'

    def __init__(self, *, present=True, failure=''):
        self.present, self.failure = present, failure

    def discover(self, scope):
        return [entity('objective', scope.project_id or 'global', scope=scope,
                       schema='objective_state.v1')] if self.present else []

    def observe(self, scope):
        def read():
            raise OSError('Synthetic source outage')
        if self.failure == 'safe':
            return self._safe(read, default=[])
        if self.failure == 'observe':
            raise OSError('Synthetic source outage')
        return [observation(row.id, self.name, {'status': 'open'}, scope=scope,
                            schema='objective_state.v1', epistemic='observed')
                for row in self.discover(scope)]


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.state_mirror import reconcile
    monkeypatch.setattr(reconcile, '_with_default_folder', lambda scope: scope)
    store = StateStore(path=str(tmp_path / 'mirror.sqlite3'))
    yield store
    store.close()


def run(db, scope, adapter):
    return sweep(owner=scope.owner, scope=scope, adapters=[adapter], store=db,
                 publisher=lambda *args, **kwargs: None)


@pytest.mark.parametrize('target', ['project-a', ''])
def test_scoped_sweep_never_retires_other_projects(db, target):
    scopes = [Scope(owner='alice', project_id=p) for p in ('project-a', 'project-b', '')]
    for scope in scopes:
        assert not run(db, scope, ScopedAdapter()).retired
    report = run(db, Scope(owner='alice', project_id=target), ScopedAdapter(present=False))
    assert len(report.retired) == 1
    for scope in scopes:
        row = ScopedAdapter().discover(scope)[0]
        assert db.get_entity(row.id).retired() == (scope.project_id == target)


@pytest.mark.parametrize('failure', ['safe', 'observe'])
def test_failed_read_is_not_evidence_of_deletion(db, failure):
    scope = Scope(owner='alice', project_id='project-a')
    run(db, scope, ScopedAdapter())
    report = run(db, scope, ScopedAdapter(present=False, failure=failure))
    assert report.adapters_failed == ('scoped-test',)
    assert not report.retired
    assert not db.get_entity(ScopedAdapter().discover(scope)[0].id).retired()
