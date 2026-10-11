from pathlib import Path

import pytest

from services.memory.skill_format import parse_frontmatter
from src.instruction_portability import preview_bundle, write_bundle


def sample(tmp_path):
    folder=tmp_path/'source';folder.mkdir()
    (folder/'references').mkdir();(folder/'references/check.md').write_text('Compare the output.\n')
    (folder/'assets').mkdir();(folder/'assets/binary.dat').write_bytes(b'\xff\x00\x80')
    markdown='---\nname: fixture\ndescription: Use when comparing a result.\nowner: alice\nsource: learned\nmanual_only: true\nlicense: MIT\nmetadata:\n  host_path: ${CLAUDE_PROJECT_DIR}\n---\n\nRead [the check](references/check.md) and compare the output.\n'
    (folder/'SKILL.md').write_text(markdown)
    return {'name':'fixture','description':'Use when comparing a result.','markdown':markdown,'path':str(folder/'SKILL.md')}


@pytest.mark.parametrize('target',['claude','codex','cursor'])
def test_bundle_writes_resources_and_is_repeatable(tmp_path,target):
    item=sample(tmp_path);original=Path(item['path']).read_bytes()
    bundle=preview_bundle([item],[],[],target)
    workspace=tmp_path/'workspace'
    first=write_bundle(workspace,bundle)
    assert first['created']==len(bundle['files']) and not first['conflicts']
    second=write_bundle(workspace,bundle)
    assert second['unchanged']==len(bundle['files'])
    binary=next(f for f in bundle['files'] if f['path'].endswith('binary.dat'))
    assert (workspace/binary['path']).read_bytes()==b'\xff\x00\x80'
    assert Path(item['path']).read_bytes()==original
    if target=='cursor':
        mdc=next(f for f in bundle['files'] if f['path'].endswith('.mdc'))
        assert '(../skills/fixture/references/check.md)' in mdc['content']
        assert (workspace/'.cursor/rules/../skills/fixture/references/check.md').is_file()


def test_codex_metadata_allowlist_and_manual_policy(tmp_path):
    bundle=preview_bundle([sample(tmp_path)],[],[],'codex')
    files={f['path']:f['content'] for f in bundle['files']}
    md=files['.agents/skills/fixture/SKILL.md'];fm,_=parse_frontmatter(md)
    assert set(fm)=={'name','description','license','metadata'}
    assert fm['license']=='MIT' and fm['metadata']['host_path']=='.'
    assert '${CLAUDE_' not in str(files)
    import yaml
    appearance=yaml.safe_load(files['.agents/skills/fixture/agents/openai.yaml'])
    assert appearance['interface']['display_name']
    assert appearance['policy']['allow_implicit_invocation'] is False
    assert bundle['warnings']


@pytest.mark.parametrize('target',['claude','cursor'])
def test_manual_skill_does_not_become_automatic(tmp_path,target):
    bundle=preview_bundle([sample(tmp_path)],[],[],target)
    text=next(f['content'] for f in bundle['files'] if f['path'].endswith(('SKILL.md','.mdc')))
    fm,_=parse_frontmatter(text)
    if target=='claude':
        assert fm['disable-model-invocation'] is True
    else:
        assert not fm['alwaysApply'] and not fm.get('description') and not fm.get('globs')


@pytest.mark.parametrize('target',['claude','codex','cursor'])
def test_rules_and_agents_are_exported_with_host_semantics(target):
    rules=[{'id':'python/testing','description':'Testing','text':'Check the result.','paths':['src/**/*.py']},
           {'id':'manual','text':'Compare the fixture.','manual':True}]
    agents=[{'slug':'reviewer','description':'Review code','prompt':'Check changed lines.','tools':['read_file']}]
    bundle=preview_bundle([],rules,agents,target);text=str(bundle)
    assert 'Check the result.' in text and 'Check changed lines.' in text
    if target=='cursor':
        fm,_=parse_frontmatter(bundle['files'][0]['content'])
        assert fm['globs']=='src/**/*.py' and fm['alwaysApply'] is False
        fm,_=parse_frontmatter(bundle['files'][1]['content'])
        assert not fm.get('description') and not fm.get('globs')
    elif target=='claude':
        fm,_=parse_frontmatter(bundle['files'][0]['content']);assert fm['paths']==['src/**/*.py']
        assert any(f['path']=='.claude/agents/reviewer.md' for f in bundle['files'])
    else:
        assert 'agent-reviewer/agents/openai.yaml' in text and bundle['warnings']


def test_existing_different_files_preserved_unless_export_overwrite_selected(tmp_path):
    bundle=preview_bundle([{'name':'fixture','description':'Use when checking','procedure':['Check']}],[],[],'claude')
    write_bundle(tmp_path,bundle)
    path=tmp_path/bundle['files'][0]['path'];path.write_text('Existing custom instructions.\n')
    receipt=write_bundle(tmp_path,bundle)
    assert receipt['conflicts']==[bundle['files'][0]['path']] and path.read_text().startswith('Existing')
    assert write_bundle(tmp_path,bundle,overwrite=True)['updated']==1


@pytest.mark.parametrize('invalid',['../outside.txt','/absolute.txt','C:/outside.txt','a/../../outside.txt'])
def test_bundle_validated_before_any_write(tmp_path,invalid):
    bundle={'files':[{'path':'valid.txt','content':'fixture'},{'path':invalid,'content':'wrong'}]}
    with pytest.raises(ValueError):write_bundle(tmp_path,bundle)
    assert not (tmp_path/'valid.txt').exists()


def test_duplicate_slugs_are_preserved_separately():
    bundle=preview_bundle([{'name':'same','procedure':['First']},{'name':'same','procedure':['Second']}],[],[],'codex')
    paths=[f['path'] for f in bundle['files']]
    assert len(paths)==len(set(paths)) and any('/same-2/' in p for p in paths)


def test_codex_appearance_has_no_foreign_host_variables():
    bundle=preview_bundle([{'name':'fixture','description':'Use ${CLAUDE_PROJECT_DIR}','procedure':['Check']}],[],[],'codex')
    assert '${CLAUDE_' not in str(bundle['files'])


def test_indented_code_body_keeps_leading_whitespace():
    markdown='---\nname: fixture\ndescription: Use when checking\n---\n\n    print("fixture")\n'
    bundle=preview_bundle([{'markdown':markdown}],[],[],'codex')
    assert '\n\n    print("fixture")\n' in bundle['files'][0]['content']


def test_cursor_reference_style_links_point_to_exported_resource(tmp_path):
    item=sample(tmp_path)
    item['markdown']+='\n[fixture]: references/check.md\n'
    bundle=preview_bundle([item],[],[],'cursor')
    assert '[fixture]: ../skills/fixture/references/check.md' in bundle['files'][0]['content']


@pytest.mark.parametrize('target',['claude','codex','cursor'])
def test_foreign_manual_only_policy_is_preserved(target):
    markdown='---\nname: fixture\ndescription: Use when checking\ndisable-model-invocation: true\n---\n\nCheck the fixture.\n'
    bundle=preview_bundle([{'markdown':markdown}],[],[],target)
    if target=='claude':
        assert parse_frontmatter(bundle['files'][0]['content'])[0]['disable-model-invocation'] is True
    elif target=='codex':
        import yaml
        assert yaml.safe_load(bundle['files'][1]['content'])['policy']['allow_implicit_invocation'] is False
    else:
        assert not parse_frontmatter(bundle['files'][0]['content'])[0].get('description')
