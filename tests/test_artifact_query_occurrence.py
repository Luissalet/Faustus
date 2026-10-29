"""Read repeated symbols through the real offloaded store and native adapter."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core import database
from src import artifact_store, tool_result_offload as offload

spec = importlib.util.spec_from_file_location(
    'occurrence_reader', Path(__file__).resolve().parents[1] / 'src/agent_tools/artifact_read_tool.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


@pytest.fixture()
def stored(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + (tmp_path / 'query.db').as_posix())
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'SessionLocal', sessionmaker(bind=engine))
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(artifact_store, 'ARTIFACT_STORE_DIR', str(tmp_path / 'store'))
    monkeypatch.setattr(offload, 'get_setting', lambda key, default=None: False if key == 'offload_search_enabled' else default)
    source = 'void First(void);\n' + '.' * 900 + '\nFirst();\n' + '.' * 900 + '\nvoid First(void) { Heal(); }'
    result = offload.offload_if_oversized(
        {'output': source}, owner='reader', session_id='own', call_id='first', threshold_chars=100)
    yield result['artifact_id']
    engine.dispose()


def execute(artifact_id, args, monkeypatch, owner='reader'):
    from src import owner_identity
    monkeypatch.setattr(owner_identity, 'effective_storage_owner', lambda *a, **kw: owner)
    return asyncio.run(reader.ReadArtifactTool().execute({'artifact_id': artifact_id, **args}, {'owner': owner}))


def test_native_reader_reaches_definition_after_declaration_and_call(stored, monkeypatch):
    result = execute(stored, {'query': 'First', 'occurrence': 3, 'context_chars': 70}, monkeypatch)
    assert 'Heal();' in result['matches'][0]['text']
    assert 'Heal' not in execute(stored, {'query': 'First'}, monkeypatch)['matches'][0]['text']
    whole = offload.read_artifact_range(stored, owner='reader')['text']
    assert result['matches'][0]['offset'] == whole.rindex('First')


def test_matches_are_nonoverlapping_and_case_insensitive(stored):
    whole = offload.read_artifact_range(stored, owner='reader')['text']
    dots = whole.index('.' * 900)
    result = offload.read_artifact_range(stored, owner='reader', query='..', occurrence=3, context_chars=0)
    assert result['matches'] == [{'offset': dots + 4, 'text': '..'}]
    mixed = offload.read_artifact_range(stored, owner='reader', query='fIRSt', occurrence=2, context_chars=0)
    assert mixed['matches'][0]['text'] == 'First'


def test_missing_later_occurrence_and_large_context(stored):
    assert offload.read_artifact_range(stored, owner='reader', query='First', occurrence=4)['matches'] == []
    whole = offload.read_artifact_range(stored, owner='reader')['text']
    assert offload.read_artifact_range(stored, owner='reader', query='First', occurrence=2, context_chars=len(whole))['matches'][0]['text'] == whole


@pytest.mark.parametrize('key,value', [('occurrence', 0), ('occurrence', True), ('occurrence', '2'), ('occurrence', 1.5), ('context_chars', -1), ('context_chars', False), ('context_chars', None)])
def test_invalid_new_options_are_errors_not_silently_ignored(stored, monkeypatch, key, value):
    assert execute(stored, {'query': 'First', key: value}, monkeypatch)['exit_code'] == 1


@pytest.mark.parametrize('args', [{'occurrence': 1}, {'context_chars': 400}, {'query': '', 'occurrence': 2}, {'json_pointer': '', 'occurrence': 2}, {'json_pointer': '', 'context_chars': 20}])
def test_query_options_require_query_mode(stored, monkeypatch, args):
    assert execute(stored, args, monkeypatch)['exit_code'] == 1


def test_owner_check_precedes_query_validation_and_no_content_leaks(stored, monkeypatch):
    denied = execute(stored, {'query': 'First', 'occurrence': 0}, monkeypatch, owner='other')
    missing = execute('occ_missing', {'query': 'First', 'occurrence': 0}, monkeypatch)
    assert denied == missing == {'error': 'artifact not found', 'exit_code': 1}


def test_json_selection_and_range_still_read_exact_stored_bytes(stored, monkeypatch):
    whole = offload.read_artifact_range(stored, owner='reader')['text']
    assert execute(stored, {'start': 11, 'end': 30}, monkeypatch)['text'] == whole[11:30]
    selected = execute(stored, {'json_pointer': '/output'}, monkeypatch)
    assert json.loads(selected['output'])['value'] == json.loads(whole)['output']


@pytest.mark.parametrize('text,query,occurrence,context,expected', [
    ('İ Entry(); middle Entry(){heal();}', 'entry', 2, 0, {'offset': 18, 'text': 'Entry'}),
    ('xİy', 'İ', 1, 0, {'offset': 1, 'text': 'İ'}),
    ('A İ B İ C', 'i\u0307', 2, 0, {'offset': 6, 'text': 'İ'}),
    ('aİbXİc', 'İ', 2, 1, {'offset': 4, 'text': 'Xİc'}),
])
def test_unicode_lowercase_expansion_keeps_original_offsets_and_extent(tmp_path, monkeypatch, text, query, occurrence, context, expected):
    from src import artifact_identity
    path = tmp_path / 'unicode.txt'
    path.write_text(text, encoding='utf8')
    monkeypatch.setattr(artifact_identity, 'path_for', lambda artifact_id, *, owner: path)
    result = offload.read_artifact_range('own_fixture', owner='reader', query=query, occurrence=occurrence, context_chars=context)
    assert result['matches'] == [expected]
