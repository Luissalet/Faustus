"""SKILL.md written for other agents (the open skill format): a folded or
literal `description`, hyphenated keys, a nested `metadata` map, and the keys
this schema does not model all survive a read and a write."""
from __future__ import annotations

from services.memory.skill_format import Skill, emit_frontmatter, parse_frontmatter

OPEN_SKILL = """---
name: pdf-processing
description: >
  Extract text and tables from PDF files, fill forms, merge documents.
  Use when working with PDF files.
license: Apache-2.0
compatibility: Requires python3 and pdfplumber
metadata:
  author: example-org
  version: "1.0"
allowed-tools: Bash(git:*) Read
---

# PDF processing

Use pdfplumber to extract text.
"""


def test_folded_description_is_one_line_of_text():
    fm, _ = parse_frontmatter(OPEN_SKILL)
    assert fm["description"] == ("Extract text and tables from PDF files, fill forms, merge documents. "
                                 "Use when working with PDF files.")


def test_literal_block_keeps_its_lines():
    fm, _ = parse_frontmatter("---\nname: x\ndescription: |\n  first line\n  second line\n\ntags: [a]\n---\nbody")
    assert fm["description"] == "first line\nsecond line"
    assert fm["tags"] == ["a"]


def test_hyphenated_keys_and_nested_map():
    fm, _ = parse_frontmatter(OPEN_SKILL)
    assert fm["allowed-tools"] == "Bash(git:*) Read"
    assert fm["metadata"] == {"author": "example-org", "version": "1.0"}
    assert fm["license"] == "Apache-2.0"


def test_block_list_still_parses():
    fm, _ = parse_frontmatter("---\nname: x\ntags:\n  - one\n  - two\n---\n")
    assert fm["tags"] == ["one", "two"]


def test_unmodelled_keys_round_trip_through_skill():
    skill = Skill.from_markdown(OPEN_SKILL)
    assert skill.name == "pdf-processing"
    assert skill.description.startswith("Extract text and tables")
    assert skill.extra == {"license": "Apache-2.0", "compatibility": "Requires python3 and pdfplumber",
                           "metadata": {"author": "example-org", "version": "1.0"},
                           "allowed-tools": "Bash(git:*) Read"}
    again = Skill.from_markdown(skill.to_markdown())
    assert again.extra == skill.extra
    assert again.description == skill.description
    assert skill.to_dict()["extra"]["license"] == "Apache-2.0"


def test_strings_that_look_like_numbers_keep_quotes():
    text = emit_frontmatter({"version": "1.0", "flag": "yes", "n": 3})
    fm, _ = parse_frontmatter("---\n" + text + "\n---\n")
    assert fm == {"version": "1.0", "flag": "yes", "n": 3}
