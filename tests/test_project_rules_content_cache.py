"""H21 cache freshness for project rules; approval coverage stays unchanged."""
import os
from dataclasses import replace
import pytest
from src import project_rules as rules


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    (root / '.git').mkdir(parents=True)
    folder = root / '.faustus' / 'rules'
    folder.mkdir(parents=True)
    path = folder / 'fixture.md'
    path.write_text('Use ALPHA convention', encoding='utf-8')
    monkeypatch.setattr(rules, '_setting', lambda key, default: False if key == 'project_rules_library_enabled' else default)
    monkeypatch.setattr(rules, '_estimate_tokens', lambda text: len(text)//4)
    with rules._BLOCK_LOCK:
        rules._BLOCK_CACHE.clear()
    return root, path


def test_same_size_mtime_content_edit_refreshes_cache(workspace):
    root, path = workspace
    before = rules.block(str(root))
    stamp = path.stat()
    path.write_text('Use OMEGA convention', encoding='utf-8')
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size
    after = rules.block(str(root))
    assert 'ALPHA' in before and 'OMEGA' in after and 'ALPHA' not in after


def test_unchanged_content_reuses_render_even_after_mtime_change(workspace, monkeypatch):
    root, path = workspace
    expected = rules.block(str(root))
    stamp = path.stat()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
    monkeypatch.setattr(rules, '_estimate_tokens', lambda text: pytest.fail('unchanged projection should reuse cache'))
    assert rules.block(str(root)) == expected


def test_removal_and_addition_change_rule_set(workspace):
    root, path = workspace
    assert 'ALPHA' in rules.block(str(root))
    path.unlink()
    assert rules.block(str(root)) == ''
    path.write_text('New rule after absence', encoding='utf-8')
    assert 'New rule after absence' in rules.block(str(root))


def test_failed_read_cannot_reuse_old_success_and_recovery_refreshes(workspace, monkeypatch):
    root, path = workspace
    assert 'ALPHA' in rules.block(str(root))
    original = rules._read_rule_file
    monkeypatch.setattr(rules, '_read_rule_file', lambda path, **kwargs: ('', 20, 'synthetic read failure'))
    assert rules.block(str(root)) == ''
    monkeypatch.setattr(rules, '_read_rule_file', original)
    assert 'ALPHA' in rules.block(str(root))


@pytest.mark.parametrize('trusted', [True, False])
def test_one_capture_drives_signature_and_render(workspace, monkeypatch, trusted):
    root, path = workspace
    original = rules.discover_project_rules
    calls = []
    def capture(workspace, **kwargs):
        calls.append(workspace)
        result = original(workspace, **kwargs)
        # Mutate after capture: this render must still correspond to its identity.
        path.write_text('Use OMEGA convention', encoding='utf-8')
        return result
    monkeypatch.setattr(rules, 'discover_project_rules', capture)
    first = rules.block(str(root), trusted=trusted)
    assert len(calls) == 1
    if trusted:
        assert 'ALPHA' in first and 'OMEGA' not in first
        assert 'OMEGA' in rules.block(str(root), trusted=trusted)
    else:
        assert 'NOT approved' in first and 'ALPHA' not in first and 'OMEGA' not in first


def test_render_order_is_part_of_cache_identity(workspace, monkeypatch):
    root, _ = workspace
    first = rules.discover_project_rules(str(root))[0]
    second = replace(first, id='second', path=str(root / '.faustus/rules/second.md'), text='Second rule')
    current = [first, second]
    monkeypatch.setattr(rules, 'discover_project_rules', lambda workspace: list(current))
    before = rules.block(str(root))
    current.reverse()
    after = rules.block(str(root))
    assert before.index('ALPHA') < before.index('Second rule')
    assert after.index('Second rule') < after.index('ALPHA')
