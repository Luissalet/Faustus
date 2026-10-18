from services.memory.skill_format import Skill
from services.memory.skills import SkillsManager


def write(root, folder, name, owner, description, bom=False):
    directory = root / 'skills/general' / folder
    directory.mkdir(parents=True, exist_ok=True)
    source = Skill(name=name, category='general', owner=owner, description=description,
                   created='2026-09-18T19:48:00Z', procedure=['Keep this original procedure.'])
    path = directory / 'SKILL.md'
    path.write_text(('\ufeff' if bom else '') + source.to_markdown(), encoding='utf-8')
    return path


def test_global_bom_skill_keeps_identity_owner_and_existing_fallback_file(tmp_path):
    source = write(tmp_path, 'iterative-cursor-loop', 'iterative-cursor-loop', '*', 'Global cursor', bom=True)
    fallback = write(tmp_path, 'skill', 'skill', 'admin', 'Different existing skill')
    originals = {path: path.read_bytes() for path in (source, fallback)}
    manager = SkillsManager(str(tmp_path))
    assert manager.backfill_owner('admin', {'admin'}) == 0
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    parsed = Skill.from_markdown(source.read_text(encoding='utf-8'))
    assert parsed.name == 'iterative-cursor-loop' and parsed.owner == '*'
    assert parsed.created == '2026-09-18T19:48:00Z'


def test_owner_backfill_preserves_original_path_when_name_collides(tmp_path):
    source = write(tmp_path, 'legacy-folder', 'same-name', None, 'Legacy procedure to retain')
    existing = write(tmp_path, 'same-name', 'same-name', 'admin', 'Existing canonical procedure')
    before = existing.read_bytes()
    manager = SkillsManager(str(tmp_path))
    assert manager.backfill_owner('admin', {'admin'}) == 1
    assert existing.read_bytes() == before
    repaired = Skill.from_markdown(source.read_text(encoding='utf-8'))
    assert repaired.owner == 'admin' and repaired.description == 'Legacy procedure to retain'
    assert repaired.procedure == ['Keep this original procedure.']
    assert manager.backfill_owner('admin', {'admin'}) == 0
