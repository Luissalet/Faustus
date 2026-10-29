"""Skills that a connected tool makes redundant (`fallback_for_tools`).

A learned skill "download a YouTube video with yt-dlp" sent the agent to bash
while Links Hoard's `media_download` tool was connected. The skill stays on
disk; it is only left out of the agent's index and matches while the tool is
there, and returns when it is not.
"""
import pytest

from services.memory.skill_format import Skill
from services.memory.skills import (
    SkillsManager, connected_tool_names, fallback_tools_for, superseded_by_tools,
)
from src import tool_utils
from src.skills_runtime import selector

OWNER = "admin"


class _FakeManager:
    def __init__(self, tools, status="connected"):
        self._tools = tools
        self._status = status

    def get_all_tools(self, disabled_map=None):
        return [{"server_id": sid, "name": name, "qualified_name": f"mcp__{sid}__{name}",
                 "is_disabled": disabled} for sid, name, disabled in self._tools]

    def get_server_status(self, server_id):
        return {"status": self._status}


def _skill(name, *, tags=(), procedure=(), extra=None, description="", as_dict=True):
    sk = _skill_obj(name, tags=tags, procedure=procedure, extra=extra, description=description)
    return sk.to_dict() if as_dict else sk


def _skill_obj(name, *, tags=(), procedure=(), extra=None, description=""):
    return Skill(name=name, description=description or name.replace("-", " "), status="published",
                 tags=list(tags), procedure=list(procedure), owner=OWNER, confidence=0.9,
                 when_to_use="user wants it", extra=dict(extra or {}))


@pytest.fixture
def manager(tmp_path):
    sm = SkillsManager(str(tmp_path))
    sm._write_skill(_skill_obj(
        "download-youtube-video-with-yt-dlp",
        tags=["yt-dlp", "youtube"],
        procedure=["Run `yt-dlp -f best <URL>`", "Transcode with ffmpeg"]))
    sm._write_skill(_skill_obj("write-a-haiku", tags=["poetry"], procedure=["Count syllables"]))
    sm._write_skill(_skill_obj(
        "explicit-fallback", procedure=["Do it by hand"], extra={"fallback_for_tools": ["media_download"]}))
    sm._write_skill(_skill_obj(
        "ytdlp-opted-out", tags=["yt-dlp"], procedure=["yt-dlp --version"], extra={"fallback_for_tools": "none"}))
    return sm


@pytest.fixture
def links_connected(monkeypatch):
    monkeypatch.setattr(tool_utils, "_mcp_manager", _FakeManager([("links", "media_download", False)]))


def _names(rows):
    return {r["name"] for r in rows}


def test_a_skill_that_mentions_yt_dlp_inherits_the_links_tool():
    assert fallback_tools_for(_skill("a", procedure=["yt-dlp -F <URL>"])) == ["media_download"]
    assert fallback_tools_for(_skill("b", tags=["poetry"])) == []


def test_an_explicit_field_wins_over_the_inferred_one():
    assert fallback_tools_for(_skill("c", tags=["yt-dlp"], extra={"fallback_for_tools": "none"})) == []
    assert fallback_tools_for(_skill("d", extra={"fallback_for_tools": "media_download"})) == ["media_download"]


def test_superseded_matches_bare_and_qualified_names():
    sk = _skill("e", extra={"fallback_for_tools": ["media_download"]})
    assert superseded_by_tools(sk, {"media_download"})
    assert superseded_by_tools(sk, {"mcp__8272ad20__media_download"})
    assert not superseded_by_tools(sk, {"save_link"})
    assert not superseded_by_tools(sk, set())
    assert not superseded_by_tools(sk, None)


def test_index_hides_the_skill_while_the_tool_is_connected(manager, links_connected):
    shown = _names(manager.index_for(owner=OWNER))
    assert "write-a-haiku" in shown
    assert "ytdlp-opted-out" in shown
    assert "download-youtube-video-with-yt-dlp" not in shown
    assert "explicit-fallback" not in shown


def test_index_brings_the_skill_back_without_the_tool(manager, monkeypatch):
    monkeypatch.setattr(tool_utils, "_mcp_manager", _FakeManager([]))
    assert "download-youtube-video-with-yt-dlp" in _names(manager.index_for(owner=OWNER))
    monkeypatch.setattr(tool_utils, "_mcp_manager", None)
    assert "download-youtube-video-with-yt-dlp" in _names(manager.index_for(owner=OWNER))


def test_a_disabled_tool_or_a_disconnected_server_does_not_count(manager, monkeypatch):
    monkeypatch.setattr(tool_utils, "_mcp_manager", _FakeManager([("links", "media_download", True)]))
    assert connected_tool_names() == set()
    monkeypatch.setattr(tool_utils, "_mcp_manager", _FakeManager([("links", "media_download", False)], status="error"))
    assert connected_tool_names() == set()
    assert "download-youtube-video-with-yt-dlp" in _names(manager.index_for(owner=OWNER))


def test_slash_invocation_still_sees_the_skill(manager, links_connected):
    assert "download-youtube-video-with-yt-dlp" in _names(manager.index_for(owner=OWNER, include_manual=True))


def test_the_skill_is_not_deleted(manager, links_connected):
    assert "download-youtube-video-with-yt-dlp" in _names(manager.load(owner=OWNER))


def test_selector_does_not_match_it_while_the_tool_is_connected(manager, links_connected, monkeypatch):
    monkeypatch.setattr(selector, "_get_mode", lambda: "lexical")
    query = "download youtube video with yt-dlp"
    matched = selector.select(manager, OWNER, query, threshold=0.0, max_items=5)
    assert "download-youtube-video-with-yt-dlp" not in _names(matched)


def test_selector_matches_it_again_without_the_tool(manager, monkeypatch):
    monkeypatch.setattr(selector, "_get_mode", lambda: "lexical")
    monkeypatch.setattr(tool_utils, "_mcp_manager", None)
    matched = selector.select(manager, OWNER, "download youtube video with yt-dlp", threshold=0.0, max_items=5)
    assert "download-youtube-video-with-yt-dlp" in _names(matched)


def test_the_field_survives_a_round_trip_through_skill_md():
    sk = _skill_obj("f", extra={"fallback_for_tools": ["media_download"]})
    again = Skill.from_markdown(sk.to_markdown())
    assert again.extra.get("fallback_for_tools") == ["media_download"]


def test_the_opt_out_survives_a_round_trip_through_skill_md():
    sk = _skill_obj("g", tags=["yt-dlp"], extra={"fallback_for_tools": "none"})
    again = Skill.from_markdown(sk.to_markdown())
    assert fallback_tools_for(again.to_dict()) == []
