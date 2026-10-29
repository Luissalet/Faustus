"""Direct source queries use the real offload store and preserve source coordinates."""
import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core import database
from src import artifact_store, tool_result_offload as offload

spec = importlib.util.spec_from_file_location('text_source_reader', Path(__file__).resolve().parents[1] / 'src/agent_tools/artifact_read_tool.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
SOURCE = 'header\r\nİ FirstBattle();\r\nvoid FirstBattle(){END();}\r\n'


@pytest.fixture
def stored_source(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + (tmp_path / 'source.db').as_posix())
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'SessionLocal', sessionmaker(bind=engine))
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(artifact_store, 'ARTIFACT_STORE_DIR', str(tmp_path / 'store'))
    monkeypatch.setattr(offload, 'get_setting', lambda key, default=None: False if key == 'offload_search_enabled' else default)
    result = offload.offload_if_oversized(
        {'output': 'FirstBattle duplicated preview', 'full_text': SOURCE,
         'other': {'nested/key': SOURCE}}, owner='reader', session_id='source-session',
        call_id='source-call', threshold_chars=50)
    assert result.get('artifact_id'), 'fixture must cross the offload threshold'
    yield result
    engine.dispose()


def execute(stored, args, monkeypatch, owner='reader'):
    from src import owner_identity
    monkeypatch.setattr(owner_identity, 'effective_storage_owner', lambda *a, **kw: owner)
    return asyncio.run(reader.ReadArtifactTool().execute({'artifact_id': stored['artifact_id'], **args}, {'owner': owner}))


def test_query_reaches_definition_in_one_call_with_source_lines(stored_source, monkeypatch):
    result = execute(stored_source, {'text_pointer': '/full_text', 'query': 'FirstBattle', 'occurrence': 2, 'context_chars': 30}, monkeypatch)
    hit = result['matches'][0]
    assert hit['offset'] == SOURCE.rindex('FirstBattle') and hit['line'] == 3
    assert hit['context_start_line'] == 1 and 'END();' in hit['text']
    assert result['total_chars'] == len(SOURCE)


def test_source_range_retains_literal_crlf_and_unicode(stored_source, monkeypatch):
    result = execute(stored_source, {'text_pointer': '/full_text', 'start': 8, 'end': 25}, monkeypatch)
    assert result['text'] == SOURCE[8:25] and result['start_line'] == 2
    hit = execute(stored_source, {'text_pointer': '/full_text', 'query': 'İ', 'context_chars': 0}, monkeypatch)['matches'][0]
    assert (hit['offset'], hit['text'], hit['line']) == (8, 'İ', 2)


def test_nested_pointer_and_missing_occurrence(stored_source, monkeypatch):
    result = execute(stored_source, {'text_pointer': '/other/nested~1key', 'query': 'END', 'context_chars': 0}, monkeypatch)
    assert result['matches'][0]['text'] == 'END'
    missing = execute(stored_source, {'text_pointer': '/full_text', 'query': 'FirstBattle', 'occurrence': 3}, monkeypatch)
    assert missing['matches'] == [] and missing['total_chars'] == len(SOURCE)


@pytest.mark.parametrize('pointer', ['/missing', '/other', '/bad~2', True, None])
def test_source_errors_are_explicit_after_ownership(stored_source, monkeypatch, pointer):
    assert execute(stored_source, {'text_pointer': pointer}, monkeypatch)['exit_code'] == 1
    assert execute(stored_source, {'text_pointer': pointer}, monkeypatch, owner='other') == {'error': 'artifact not found', 'exit_code': 1}


def test_plain_query_still_uses_stored_result_coordinates(stored_source, monkeypatch):
    whole = offload.read_artifact_range(stored_source['artifact_id'], owner='reader')['text']
    result = execute(stored_source, {'query': 'FirstBattle', 'context_chars': 0}, monkeypatch)
    assert result == {'artifact_id': stored_source['artifact_id'], 'query': 'FirstBattle', 'matches': [{'offset': whole.index('FirstBattle'), 'text': 'FirstBattle'}], 'total_chars': len(whole)}
