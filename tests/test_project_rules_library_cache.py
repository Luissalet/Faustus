"""H21 library TTL and enabled state must reach cached prompt blocks."""
from types import SimpleNamespace
import pytest
from src import project_rules as rules


@pytest.fixture
def library_fixture(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    (root / '.git').mkdir(parents=True)
    lib = tmp_path / 'library'
    (lib / 'common').mkdir(parents=True)
    path = lib / 'common' / 'style.md'
    path.write_text('---\nid: common/style\ntitle: Style\npriority: 20\n---\n\n- ALPHA library rule\n', encoding='utf-8')
    clock = {'now': 100.0}
    settings = {'project_rules_library_enabled': True}
    monkeypatch.setattr(rules, 'LIBRARY_DIR', str(lib))
    monkeypatch.setattr(rules, '_LIBRARY_CACHE', None)
    monkeypatch.setattr(rules, '_BLOCK_CACHE', {})
    monkeypatch.setattr(rules, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    monkeypatch.setattr(rules, '_setting', lambda key, default: settings.get(key, default))
    monkeypatch.setattr(rules, '_estimate_tokens', lambda text: len(text)//4)
    return root, path, clock, settings


def test_expired_library_content_refreshes_unchanged_workspace(library_fixture):
    root, path, clock, _ = library_fixture
    before = rules.block(str(root))
    assert 'ALPHA' in before
    path.write_text(path.read_text(encoding='utf-8').replace('ALPHA', 'OMEGA'), encoding='utf-8')
    clock['now'] += rules._LIBRARY_TTL_S - .1
    assert rules.block(str(root)) == before
    clock['now'] += .2
    after = rules.block(str(root))
    assert 'OMEGA' in after and 'ALPHA' not in after


def test_library_enabled_setting_changes_cached_block(library_fixture):
    root, _, _, settings = library_fixture
    assert 'ALPHA' in rules.block(str(root))
    settings['project_rules_library_enabled'] = False
    assert rules.block(str(root)) == ''
    settings['project_rules_library_enabled'] = True
    assert 'ALPHA' in rules.block(str(root))


def test_disabled_library_is_not_read(library_fixture, monkeypatch):
    root, _, _, settings = library_fixture
    settings['project_rules_library_enabled'] = False
    monkeypatch.setattr(rules, '_library_rules', lambda: pytest.fail('disabled library must not be loaded'))
    assert rules.block(str(root)) == ''


def test_library_removal_and_recovery_after_ttl(library_fixture):
    root, path, clock, _ = library_fixture
    original = path.read_text(encoding='utf-8')
    assert 'ALPHA' in rules.block(str(root))
    path.unlink()
    clock['now'] += rules._LIBRARY_TTL_S + 1
    assert rules.block(str(root)) == ''
    path.write_text(original, encoding='utf-8')
    clock['now'] += rules._LIBRARY_TTL_S + 1
    assert 'ALPHA' in rules.block(str(root))


def test_unchanged_expired_snapshot_reuses_render(library_fixture, monkeypatch):
    root, _, clock, _ = library_fixture
    expected = rules.block(str(root))
    clock['now'] += rules._LIBRARY_TTL_S + 1
    monkeypatch.setattr(rules, '_estimate_tokens', lambda text: pytest.fail('identical snapshot must reuse rendered block'))
    assert rules.block(str(root)) == expected


def test_single_library_snapshot_drives_identity_and_render(library_fixture, monkeypatch):
    root, path, _, _ = library_fixture
    original = rules._library_rules
    calls = []
    def captured():
        calls.append(True)
        snapshot = original()
        path.write_text(path.read_text(encoding='utf-8').replace('ALPHA', 'OMEGA'), encoding='utf-8')
        return snapshot
    monkeypatch.setattr(rules, '_library_rules', captured)
    first = rules.block(str(root))
    assert len(calls) == 1 and 'ALPHA' in first and 'OMEGA' not in first
