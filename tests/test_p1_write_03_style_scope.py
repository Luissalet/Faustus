"""
tests/test_p1_write_03_style_scope.py — WRITE-03, lote 53.

Acceptance line: "Una regla de novela no contamina correos profesionales;
desactivar estilo realmente lo retira del contexto siguiente." Both halves
pinned against `src/writing_style.py::compile_context`, the one function a
system-prompt builder would call for the NEXT turn's context.
"""
from __future__ import annotations

from src.writing_style import (
    StyleRule,
    add_version,
    compile_context,
    effective_rules,
    from_learned_preset,
    new_guide,
)


def test_a_novel_style_guide_does_not_reach_an_email_scope():
    guide = new_guide("Gothic narrator", scope=["project:my-novel"])
    add_version(guide, [StyleRule(text="Favor long, clause-heavy sentences.",
                                  prohibitions=["Never use emoji."])])
    assert compile_context(guide, target_scope="email") is None
    novel_context = compile_context(guide, target_scope="project:my-novel")
    assert novel_context is not None
    assert "long, clause-heavy" in novel_context
    assert "Never use emoji" in novel_context


def test_disabling_the_guide_removes_it_from_the_next_context_build():
    guide = new_guide("Gothic narrator", scope=["project:my-novel"])
    add_version(guide, [StyleRule(text="Favor long sentences.")])
    assert compile_context(guide, target_scope="project:my-novel") is not None
    guide.enabled = False
    assert compile_context(guide, target_scope="project:my-novel") is None


def test_a_wildcard_scope_is_the_explicit_opt_in_to_everywhere():
    guide = new_guide("House style", scope=["*"])
    add_version(guide, [StyleRule(text="Use Oxford commas.")])
    assert compile_context(guide, target_scope="email") is not None
    assert compile_context(guide, target_scope="project:my-novel") is not None


def test_lowering_intensity_drops_rules_below_their_floor():
    guide = new_guide("Voice", scope=["*"], )
    add_version(guide, [
        StyleRule(text="Always keep sentences short.", min_intensity=0.0),
        StyleRule(text="Use archaic diction sparingly.", min_intensity=0.8),
    ])
    guide.intensity = 0.5
    effective = [r.text for r in effective_rules(guide)]
    assert "Always keep sentences short." in effective
    assert "Use archaic diction sparingly." not in effective

    guide.intensity = 0.9
    effective = [r.text for r in effective_rules(guide)]
    assert "Use archaic diction sparingly." in effective


def test_versions_are_kept_not_overwritten():
    guide = new_guide("Voice", scope=["*"])
    add_version(guide, [StyleRule(text="v1 rule")])
    add_version(guide, [StyleRule(text="v2 rule")])
    assert len(guide.versions) == 2
    assert guide.versions[0].rules[0].text == "v1 rule"
    assert guide.current().rules[0].text == "v2 rule"


def test_from_learned_preset_wraps_the_existing_derive_flow_output_without_a_model_call():
    """`rules_text` is exactly the shape `/api/presets/style/derive` already
    returns (`studio/src/screens/studio/StyleLab.tsx`'s own `body.rules`) —
    one flat string, here turned into a scoped guide with no model call."""
    rules_text = "- Keep dialogue terse.\n- Avoid adverbs in dialogue tags.\n"
    guide = from_learned_preset("Derived voice", rules_text=rules_text,
                                scope=["project:my-novel"])
    assert guide.current().source == "derived"
    texts = [r.text for r in guide.current().rules]
    assert "Keep dialogue terse." in texts
    assert "Avoid adverbs in dialogue tags." in texts
    assert compile_context(guide, target_scope="email") is None
