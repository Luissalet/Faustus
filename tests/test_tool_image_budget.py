"""Images cost context, and old tool images are pruned from the prompt.

``estimate_tokens`` used to count 0 for an ``image_url`` block, so a run that
took a screenshot every round piled up megabytes of base64 the trim/compaction
gates could not see. Now each image block is charged a flat ~1200 tokens
(``IMAGE_BLOCK_TOKENS``), and ``trim_for_context`` keeps only the LAST
``agent_keep_images`` tool-sourced image blocks — earlier ones are replaced by
the text ``[earlier image omitted]``. User-uploaded images are never touched.
"""
from __future__ import annotations

import pytest

from src import context_compactor as cc
from src.model_context import IMAGE_BLOCK_TOKENS, estimate_tokens


def _img_msg(tool: str, tag: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"[image from {tool}]"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tag}"}},
        ],
        "metadata": {"trusted": False, "source": f"tool result: {tool}", "tool_gate_untrusted": True},
    }


def _user_upload(tag: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tag}"}},
        ],
    }


def _image_tags(messages):
    tags = []
    for m in messages:
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "image_url":
                    tags.append(b["image_url"]["url"].rpartition(",")[2])
    return tags


# ── estimate_tokens ────────────────────────────────────────────────────────

def test_image_block_is_charged():
    assert IMAGE_BLOCK_TOKENS >= 500
    text_only = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    with_image = [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    assert estimate_tokens(with_image) == estimate_tokens(text_only) + IMAGE_BLOCK_TOKENS


def test_two_images_cost_twice():
    msg = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
    ]}]
    assert estimate_tokens(msg) == 4 + 2 * IMAGE_BLOCK_TOKENS


def test_image_cost_does_not_scale_with_base64_length():
    small = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    huge = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200000}}]}]
    assert estimate_tokens(small) == estimate_tokens(huge)


# ── prune_tool_images ──────────────────────────────────────────────────────

def test_keeps_only_last_tool_image():
    msgs = [
        {"role": "system", "content": "sys"},
        _img_msg("desktop_screenshot", "ONE"),
        {"role": "assistant", "content": "clicking"},
        _img_msg("desktop_screenshot", "TWO"),
        {"role": "assistant", "content": "typing"},
        _img_msg("mcp__builtin_browser__browser_take_screenshot", "THREE"),
    ]
    out = cc.prune_tool_images(msgs, keep=1)
    assert _image_tags(out) == ["THREE"]
    # Older ones became a plain text marker in place (same position, same role).
    assert out[1]["role"] == "user"
    assert out[1]["content"] == [
        {"type": "text", "text": "[image from desktop_screenshot]"},
        {"type": "text", "text": cc.EARLIER_IMAGE_OMITTED},
    ]
    assert out[1]["metadata"] == msgs[1]["metadata"]
    # Input list untouched (the loop keeps its own copy).
    assert _image_tags(msgs) == ["ONE", "TWO", "THREE"]


def test_keep_two():
    msgs = [_img_msg("t", "A"), _img_msg("t", "B"), _img_msg("t", "C")]
    assert _image_tags(cc.prune_tool_images(msgs, keep=2)) == ["B", "C"]


def test_keep_zero_drops_all_tool_images():
    msgs = [_img_msg("t", "A"), _img_msg("t", "B")]
    assert _image_tags(cc.prune_tool_images(msgs, keep=0)) == []


def test_negative_keep_means_unlimited():
    msgs = [_img_msg("t", "A"), _img_msg("t", "B")]
    assert cc.prune_tool_images(msgs, keep=-1) is msgs


def test_user_uploads_are_never_pruned():
    msgs = [_user_upload("U1"), _img_msg("t", "A"), _user_upload("U2"), _img_msg("t", "B")]
    assert _image_tags(cc.prune_tool_images(msgs, keep=1)) == ["U1", "U2", "B"]


def test_nothing_to_prune_returns_same_object():
    msgs = [{"role": "user", "content": "hi"}, _img_msg("t", "A")]
    assert cc.prune_tool_images(msgs, keep=1) is msgs


# ── trim_for_context integration ───────────────────────────────────────────

def test_trim_for_context_prunes_old_tool_images(monkeypatch):
    monkeypatch.setattr(cc, "get_setting", lambda key, default=None: 1 if key == "agent_keep_images" else default, raising=False)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "take screenshots"},
        _img_msg("desktop_screenshot", "ONE"),
        {"role": "assistant", "content": "ok"},
        _img_msg("desktop_screenshot", "TWO"),
        {"role": "user", "content": "now?"},
    ]
    # Generous budget: nothing else would be trimmed.
    out = cc.trim_for_context(msgs, 100000, reserve_tokens=10)
    assert _image_tags(out) == ["TWO"]
    assert estimate_tokens(out) == estimate_tokens(msgs) - IMAGE_BLOCK_TOKENS + 4


def test_trim_for_context_honours_keep_setting(monkeypatch):
    monkeypatch.setattr(cc, "get_setting", lambda key, default=None: 3 if key == "agent_keep_images" else default, raising=False)
    msgs = [_img_msg("t", "A"), _img_msg("t", "B"), {"role": "user", "content": "x"}]
    out = cc.trim_for_context(msgs, 100000, reserve_tokens=10)
    assert _image_tags(out) == ["A", "B"]


def test_trim_for_context_setting_failure_is_safe(monkeypatch):
    def _boom(key, default=None):
        raise RuntimeError("no settings here")
    monkeypatch.setattr(cc, "get_setting", _boom, raising=False)
    msgs = [_img_msg("t", "A"), _img_msg("t", "B"), {"role": "user", "content": "x"}]
    out = cc.trim_for_context(msgs, 100000, reserve_tokens=10)
    assert _image_tags(out) == ["B"]  # default keep=1
