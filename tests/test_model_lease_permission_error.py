import os
import pytest


def test_permission_error_does_not_retry_temporary_file_creation(monkeypatch,tmp_path):
    from src import model_lease
    calls=[]
    def denied(*args,**kwargs):
        calls.append(args)
        if len(calls)>1:raise AssertionError('Permission denial retried')
        raise PermissionError('Fixture denied directory')
    monkeypatch.setattr(os,'open',denied)
    with pytest.raises(PermissionError):
        model_lease._atomic_write(str(tmp_path/'lease.json'),{'instance':'test'})
    assert len(calls)==1 and not (tmp_path/'lease.json').exists()
