"""Desktop control tool family (FAUSTUS): see the screen, move the mouse.

Seven builtin tools in ``src/agent_tools/desktop_tools.py``:

    desktop_screenshot, desktop_list_windows, desktop_focus_window,
    desktop_click, desktop_type, desktop_key, desktop_scroll

They run on the machine the server runs on (the owner's Windows desktop as an
interactive process). Platform access goes through a backend object so these
tests never touch a real display: a fake backend records calls and returns
synthetic images. Nothing here asserts Windows-only behaviour.

Registration is asserted in every place a tool has to exist: dispatch
(TOOL_HANDLERS/TOOL_TAGS), function schemas, prompt sections, the ToolIndex
description registry + keyword hints, capability classification, the non-admin
blocklist and the admin UI catalogue.

Gate: the five control tools are ``ALWAYS_APPROVE_TOOLS`` — an approval card on
EVERY call regardless of task/chat-scope approvals — unless
``desktop_control_mode`` is ``ask_task`` (normal scoped gate) or ``off`` (not
offered, and refused if called anyway).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re

import pytest

import src.agent_tools  # noqa: F401  - resolves circular imports first
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS, ToolBlock
from src.agent_tools import desktop_tools as dt
from src.tool_capabilities import (
    ALWAYS_APPROVE_TOOLS,
    ResultIntegrity,
    ToolEffect,
    ToolRunSecurityContext,
    capabilities_for_tool,
    tool_requires_per_call_approval,
)
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DESKTOP_TOOLS = {
    "desktop_screenshot", "desktop_list_windows", "desktop_focus_window",
    "desktop_click", "desktop_type", "desktop_key", "desktop_scroll",
}
CONTROL_TOOLS = {"desktop_click", "desktop_type", "desktop_key", "desktop_scroll", "desktop_focus_window"}


def _image(width, height, color=(30, 120, 200)):
    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    # A non-uniform pixel so the blank-capture check does not fire.
    img.putpixel((0, 0), (255, 255, 255))
    return img


class FakeBackend(dt.DesktopBackend):
    name = "fake"

    def __init__(self, width=1920, height=1080, image=None, blank=False):
        self.calls = []
        self._w, self._h = width, height
        self._image = image
        self._blank = blank
        self.monitors = [{"index": 0, "left": 0, "top": 0, "width": width, "height": height, "primary": True}]

    def available(self):
        return True, ""

    def screen_size(self):
        return self._w, self._h

    def list_monitors(self):
        return list(self.monitors)

    def grab(self, region):
        self.calls.append(("grab", region))
        if self._image is not None:
            return self._image
        left, top, w, h = region
        from PIL import Image
        if self._blank:
            return Image.new("RGB", (w, h), (0, 0, 0))
        return _image(w, h)

    _WINDOWS = [
        {"title": "Notepad - notes.txt", "handle": 1, "rect": [0, 0, 800, 600], "foreground": True},
        {"title": "Mozilla Firefox", "handle": 2, "rect": [100, 100, 1600, 900], "foreground": False},
    ]

    def list_windows(self):
        self.calls.append(("list_windows",))
        return [dict(w) for w in self._WINDOWS]

    def focus_window(self, title):
        self.calls.append(("focus_window", title))
        wins = [w for w in self._WINDOWS if title.lower() in w["title"].lower()]
        if not wins:
            raise dt.DesktopError(f"no window matches {title!r}")
        return wins[0]

    def click(self, x, y, button):
        self.calls.append(("click", x, y, button))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def key_combo(self, keys):
        self.calls.append(("key_combo", tuple(keys)))

    def scroll(self, x, y, dy):
        self.calls.append(("scroll", x, y, dy))


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(dt, "get_backend", lambda: fake)
    dt.reset_capture_state()
    return fake


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    values = {"desktop_control_mode": "ask_each", "agent_tool_image_max_px": 1280,
              "agent_tool_images": True, "agent_tool_preflight": True}

    def _get(key, default=None):
        return values.get(key, default)

    import src.tool_capabilities as tc
    import src.tool_execution as te
    from src import tool_images
    monkeypatch.setattr(dt, "get_setting", _get, raising=False)
    monkeypatch.setattr(tc, "get_setting", _get, raising=False)
    monkeypatch.setattr(tool_images, "get_setting", _get, raising=False)
    # The desktop tools are admin/single-user only (see test_non_admin_blocked);
    # the dispatch tests below exercise them as the owner of the box.
    monkeypatch.setattr(te, "_owner_is_admin", lambda owner: True)
    return values


def _run(tool, args=None, **kw):
    content = json.dumps(args) if isinstance(args, dict) else (args or "{}")
    return asyncio.run(execute_tool_block(
        ToolBlock(tool, content),
        security_context=kw.pop("security_context", NO_TOOL_SECURITY_CONTEXT),
        **kw,
    ))


# ── Registration (9 points) ────────────────────────────────────────────────

def test_registered_in_dispatch_tables():
    for name in DESKTOP_TOOLS:
        assert name in TOOL_TAGS, name
        assert name in TOOL_HANDLERS, name
    assert dt.DESKTOP_TOOLS == frozenset(DESKTOP_TOOLS)
    assert dt.DESKTOP_CONTROL_TOOLS == frozenset(CONTROL_TOOLS)


def test_registered_in_function_schemas():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    by_name = {s["function"]["name"]: s["function"] for s in FUNCTION_TOOL_SCHEMAS}
    for name in DESKTOP_TOOLS:
        assert name in by_name, name
    props = by_name["desktop_click"]["parameters"]["properties"]
    assert {"x", "y", "button"} <= set(props)
    assert by_name["desktop_click"]["parameters"]["required"] == ["x", "y"]
    assert "screenshot" in by_name["desktop_click"]["description"].lower()
    assert by_name["desktop_key"]["parameters"]["required"] == ["combo"]
    assert by_name["desktop_type"]["parameters"]["required"] == ["text"]
    assert by_name["desktop_scroll"]["parameters"]["required"] == ["dy"]
    assert by_name["desktop_focus_window"]["parameters"]["required"] == ["title"]
    assert set(by_name["desktop_screenshot"]["parameters"]["properties"]) >= {"monitor", "region"}


def test_registered_in_prompt_sections_and_index():
    from src.agent_loop import TOOL_SECTIONS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS

    for name in DESKTOP_TOOLS:
        assert name in TOOL_SECTIONS, name
        assert name in BUILTIN_TOOL_DESCRIPTIONS, name
        assert len(BUILTIN_TOOL_DESCRIPTIONS[name]) > 40
    assert "pixel" in TOOL_SECTIONS["desktop_click"].lower()


def test_keyword_hints_surface_desktop_tools_in_spanish_and_english():
    from src.tool_index import ToolIndex

    hints = ToolIndex._KEYWORD_HINTS
    joined = " ".join(" ".join(sorted(k)) for k in hints)
    for kw in ["pantalla", "captura", "screenshot", "ventana", "clic", "teclea", "escritorio", "desktop"]:
        assert kw in joined, kw
    matched = set()
    for keywords, tools in hints.items():
        if "screenshot" in keywords:
            matched |= set(tools)
    assert DESKTOP_TOOLS <= matched


def test_capability_classification():
    for name in ("desktop_screenshot", "desktop_list_windows"):
        caps = capabilities_for_tool(name)
        assert caps.known
        assert caps.effects == frozenset({ToolEffect.READ_PRIVATE})
        assert caps.result_integrity is ResultIntegrity.EXTERNAL_UNTRUSTED
    for name in CONTROL_TOOLS:
        caps = capabilities_for_tool(name)
        assert caps.known
        assert caps.effects == frozenset({ToolEffect.EXTERNAL_SIDE_EFFECT})
        assert caps.result_integrity is ResultIntegrity.EXTERNAL_UNTRUSTED


def test_non_admin_blocked():
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, is_public_blocked_tool

    for name in DESKTOP_TOOLS:
        assert name in NON_ADMIN_BLOCKED_TOOLS
        assert is_public_blocked_tool(name)


def test_admin_ui_catalogue_has_desktop_category():
    src = open(os.path.join(ROOT, "static", "js", "admin.js"), encoding="utf-8").read()
    for name in DESKTOP_TOOLS:
        assert re.search(rf"^\s*{name}:\s*\{{.*cat:\s*'Desktop'", src, re.MULTILINE), name
    assert "'Desktop'" in src.split("const catOrder = ")[1].split("\n")[0]


def test_optional_accelerators_listed():
    text = open(os.path.join(ROOT, "requirements-optional.txt"), encoding="utf-8").read()
    assert "mss" in text
    assert "pyautogui" in text


def test_always_approve_set():
    assert ALWAYS_APPROVE_TOOLS == frozenset(CONTROL_TOOLS)
    assert "desktop_screenshot" not in ALWAYS_APPROVE_TOOLS


def test_settings_defaults():
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["desktop_control_mode"] == "ask_each"
    assert DEFAULT_SETTINGS["agent_tool_images"] is True
    assert DEFAULT_SETTINGS["agent_tool_image_max_px"] == 1280
    assert DEFAULT_SETTINGS["agent_keep_images"] == 1
    keys = list(DEFAULT_SETTINGS)
    i = keys.index("vision_model_fallbacks")
    assert keys[i + 1:i + 5] == ["agent_tool_images", "agent_tool_image_max_px", "agent_keep_images", "desktop_control_mode"]


# ── desktop_screenshot ─────────────────────────────────────────────────────

def test_screenshot_returns_images_and_data_url(backend):
    desc, result = _run("desktop_screenshot")
    assert result["exit_code"] == 0, result
    assert result["images"][0]["mimeType"] in ("image/png", "image/jpeg")
    b64 = result["images"][0]["data"]
    base64.b64decode(b64)  # valid base64
    assert result["screenshot"] == f"data:{result['images'][0]['mimeType']};base64,{b64}"
    assert result["screen"] == {"width": 1920, "height": 1080}
    assert result["image"] == {"width": 1280, "height": 720}
    assert result["scale"] == pytest.approx(1280 / 1920, rel=1e-3)
    assert result["monitor"] == 0
    assert "1920x1080" in result["output"]
    assert "1280x720" in result["output"]
    assert backend.calls == [("grab", (0, 0, 1920, 1080))]


def test_screenshot_small_screen_is_not_upscaled(monkeypatch):
    fake = FakeBackend(800, 600)
    monkeypatch.setattr(dt, "get_backend", lambda: fake)
    _, result = _run("desktop_screenshot")
    assert result["image"] == {"width": 800, "height": 600}
    assert result["scale"] == 1.0
    assert result["images"][0]["mimeType"] == "image/png"


def test_screenshot_region(backend):
    _, result = _run("desktop_screenshot", {"region": [100, 50, 400, 300]})
    assert result["exit_code"] == 0, result
    assert backend.calls == [("grab", (100, 50, 400, 300))]
    assert result["region"] == [100, 50, 400, 300]
    assert result["image"] == {"width": 400, "height": 300}


def test_screenshot_invalid_region(backend):
    _, result = _run("desktop_screenshot", {"region": [0, 0, 0, 10]})
    assert result["exit_code"] == 1
    assert "region" in result["error"]
    assert backend.calls == []


def test_screenshot_monitor_index(backend):
    backend.monitors.append({"index": 1, "left": 1920, "top": 0, "width": 1280, "height": 1024, "primary": False})
    _, result = _run("desktop_screenshot", {"monitor": 1})
    assert backend.calls == [("grab", (1920, 0, 1280, 1024))]
    assert result["monitor"] == 1


def test_screenshot_unknown_monitor(backend):
    _, result = _run("desktop_screenshot", {"monitor": 7})
    assert result["exit_code"] == 1
    assert "monitor" in result["error"]


def test_blank_capture_is_an_error(monkeypatch):
    fake = FakeBackend(blank=True)
    monkeypatch.setattr(dt, "get_backend", lambda: fake)
    _, result = _run("desktop_screenshot")
    assert result["exit_code"] == 1
    assert "images" not in result
    assert "blank" in result["error"].lower()


def test_screenshot_respects_max_px_setting(backend, _settings):
    from PIL import Image

    _settings["agent_tool_image_max_px"] = 320
    _, result = _run("desktop_screenshot")
    img = Image.open(io.BytesIO(base64.b64decode(result["images"][0]["data"])))
    assert img.size == (320, 180)
    assert result["scale"] == pytest.approx(320 / 1920, rel=1e-3)


# ── coordinate mapping ─────────────────────────────────────────────────────

def test_click_maps_screenshot_pixels_back_to_screen(backend):
    _run("desktop_screenshot")  # scale 2/3
    _, result = _run("desktop_click", {"x": 640, "y": 360})
    assert result["exit_code"] == 0, result
    assert backend.calls[-1] == ("click", 960, 540, "left")
    assert result["screen_xy"] == [960, 540]


def test_click_with_region_offset(backend):
    _run("desktop_screenshot", {"region": [100, 50, 400, 300]})  # no downscale
    _run("desktop_click", {"x": 10, "y": 20, "button": "right"})
    assert backend.calls[-1] == ("click", 110, 70, "right")


def test_click_screen_coordinates_bypass_mapping(backend):
    _run("desktop_screenshot")
    _run("desktop_click", {"x": 100, "y": 100, "coords": "screen"})
    assert backend.calls[-1] == ("click", 100, 100, "left")


def test_click_without_screenshot_uses_screen_coordinates(backend):
    _run("desktop_click", {"x": 5, "y": 6, "button": "double"})
    assert backend.calls[-1] == ("click", 5, 6, "double")


def test_click_rejects_bad_button_and_missing_coordinates(backend):
    _, result = _run("desktop_click", {"x": 1, "y": 2, "button": "middle-ish"})
    assert result["exit_code"] == 1 and "button" in result["error"]
    _, result = _run("desktop_click", {"x": "abc"})
    assert result["exit_code"] == 1
    assert ("click",) not in [c[:1] for c in backend.calls]


def test_click_outside_screen_is_rejected(backend):
    _, result = _run("desktop_click", {"x": 99999, "y": 5, "coords": "screen"})
    assert result["exit_code"] == 1
    assert "outside" in result["error"]


def test_scroll_maps_coordinates_too(backend):
    _run("desktop_screenshot")
    _, result = _run("desktop_scroll", {"x": 640, "y": 360, "dy": 3})
    assert result["exit_code"] == 0
    assert backend.calls[-1] == ("scroll", 960, 540, 3)


def test_scroll_without_position_uses_center(backend):
    _, result = _run("desktop_scroll", {"dy": -2})
    assert result["exit_code"] == 0
    assert backend.calls[-1] == ("scroll", 960, 540, -2)


# ── type / key / focus / list ──────────────────────────────────────────────

def test_type_text(backend):
    _, result = _run("desktop_type", {"text": "hola mundo\n"})
    assert result["exit_code"] == 0
    assert backend.calls == [("type_text", "hola mundo\n")]
    assert "11" in result["output"]


def test_type_rejects_empty(backend):
    _, result = _run("desktop_type", {"text": ""})
    assert result["exit_code"] == 1
    assert backend.calls == []


def test_key_combo(backend):
    _, result = _run("desktop_key", {"combo": "Ctrl+Shift+S"})
    assert result["exit_code"] == 0, result
    assert backend.calls == [("key_combo", ("ctrl", "shift", "s"))]


def test_key_aliases_and_validation():
    assert dt.parse_key_combo("control+s") == ["ctrl", "s"]
    assert dt.parse_key_combo("return") == ["enter"]
    assert dt.parse_key_combo("alt + tab") == ["alt", "tab"]
    assert dt.parse_key_combo("win+d") == ["win", "d"]
    assert dt.parse_key_combo("F5") == ["f5"]
    with pytest.raises(dt.DesktopError):
        dt.parse_key_combo("ctrl+")
    with pytest.raises(dt.DesktopError):
        dt.parse_key_combo("bogus_key")
    with pytest.raises(dt.DesktopError):
        dt.parse_key_combo("")


def test_focus_window(backend):
    _, result = _run("desktop_focus_window", {"title": "firefox"})
    assert result["exit_code"] == 0, result
    assert backend.calls == [("focus_window", "firefox")]
    assert "Mozilla Firefox" in result["output"]


def test_focus_window_no_match(backend):
    _, result = _run("desktop_focus_window", {"title": "nope"})
    assert result["exit_code"] == 1
    assert "nope" in result["error"]


def test_list_windows(backend):
    _, result = _run("desktop_list_windows")
    assert result["exit_code"] == 0
    assert "Notepad - notes.txt" in result["output"]
    assert "Mozilla Firefox" in result["output"]
    assert result["windows"][0]["foreground"] is True
    assert "foreground" in result["output"].lower() or "*" in result["output"]


# ── unsupported platform / headless ────────────────────────────────────────

def test_unsupported_backend_fails_clearly(monkeypatch):
    monkeypatch.setattr(dt, "get_backend", lambda: dt.UnsupportedBackend("no display (DISPLAY is not set)"))
    for tool, args in [
        ("desktop_screenshot", {}), ("desktop_list_windows", {}),
        ("desktop_focus_window", {"title": "x"}), ("desktop_click", {"x": 1, "y": 1}),
        ("desktop_type", {"text": "x"}), ("desktop_key", {"combo": "enter"}),
        ("desktop_scroll", {"dy": 1}),
    ]:
        _, result = _run(tool, args)
        assert result["exit_code"] == 1, tool
        assert "DISPLAY" in result["error"], tool


def test_backend_exception_is_a_result_not_a_raise(monkeypatch):
    class Boom(FakeBackend):
        def grab(self, region):
            raise OSError("screen grab failed: session 0")

    monkeypatch.setattr(dt, "get_backend", lambda: Boom())
    _, result = _run("desktop_screenshot")
    assert result["exit_code"] == 1
    assert "session 0" in result["error"]


def test_availability_reports_unsupported_when_no_backend(monkeypatch):
    monkeypatch.setattr(dt, "get_backend", lambda: dt.UnsupportedBackend("headless"))
    ok, reason = dt.desktop_availability()
    assert ok is False and "headless" in reason


# ── desktop_control_mode ───────────────────────────────────────────────────

def test_mode_off_refuses_control_tools_but_not_screenshot(backend, _settings):
    _settings["desktop_control_mode"] = "off"
    _, result = _run("desktop_click", {"x": 1, "y": 1})
    assert result["exit_code"] == 1
    assert "desktop_control_mode" in result["error"]
    assert backend.calls == []
    _, result = _run("desktop_screenshot")
    assert result["exit_code"] == 0


def test_preflight_prunes_control_tools_when_off(monkeypatch, _settings, backend):
    from src.tool_preflight import PreflightContext, unusable_tools

    monkeypatch.setattr("src.tool_preflight.get_setting", _settings_getter(_settings), raising=False)
    _settings["desktop_control_mode"] = "off"
    pruned = unusable_tools(PreflightContext(tools=frozenset(DESKTOP_TOOLS | {"bash"})))
    assert set(pruned) == CONTROL_TOOLS
    assert all("desktop_control_mode" in r for r in pruned.values())
    _settings["desktop_control_mode"] = "ask_each"
    assert unusable_tools(PreflightContext(tools=frozenset(DESKTOP_TOOLS))) == {}


def test_preflight_prunes_all_desktop_tools_when_unsupported(monkeypatch, _settings):
    from src.tool_preflight import PreflightContext, unusable_tools

    monkeypatch.setattr("src.tool_preflight.get_setting", _settings_getter(_settings), raising=False)
    monkeypatch.setattr(dt, "get_backend", lambda: dt.UnsupportedBackend("no display"))
    pruned = unusable_tools(PreflightContext(tools=frozenset(DESKTOP_TOOLS | {"bash"})))
    assert set(pruned) == DESKTOP_TOOLS
    assert all("no display" in r for r in pruned.values())


def _settings_getter(values):
    def _get(key, default=None):
        return values.get(key, default)
    return _get


# ── ALWAYS_APPROVE gate ────────────────────────────────────────────────────

def test_control_tools_need_approval_on_every_call_even_when_bypassed(_settings):
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    for name in CONTROL_TOOLS:
        decision = ctx.decision_for(name, '{"x": 1, "y": 1}')
        assert decision.allowed is False, name
        assert name in decision.reason
        assert tool_requires_per_call_approval(name)
    # Read-only desktop tools follow the normal rules.
    assert ctx.decision_for("desktop_screenshot").allowed is True
    assert ToolRunSecurityContext().decision_for("desktop_screenshot").allowed is True
    assert tool_requires_per_call_approval("desktop_screenshot") is False


def test_control_tools_need_approval_even_without_external_context(_settings):
    ctx = ToolRunSecurityContext()
    assert ctx.external_untrusted_context_seen is False
    assert ctx.decision_for("desktop_click").allowed is False


def test_ask_task_mode_uses_normal_scoped_gate(_settings):
    _settings["desktop_control_mode"] = "ask_task"
    assert ToolRunSecurityContext().decision_for("desktop_click").allowed is True
    armed = ToolRunSecurityContext(external_untrusted_context_seen=True)
    assert armed.decision_for("desktop_click").allowed is False
    bypassed = ToolRunSecurityContext(external_untrusted_context_seen=True, approval_gate_bypassed=True)
    assert bypassed.decision_for("desktop_click").allowed is True
    assert tool_requires_per_call_approval("desktop_click") is False


def test_unknown_mode_falls_back_to_ask_each(_settings):
    _settings["desktop_control_mode"] = "whatever"
    assert ToolRunSecurityContext(approval_gate_bypassed=True).decision_for("desktop_key").allowed is False


@pytest.mark.asyncio
async def test_dispatcher_blocks_control_tool_without_approval(backend):
    ctx = ToolRunSecurityContext()
    desc, result = await execute_tool_block(
        ToolBlock("desktop_click", '{"x": 1, "y": 1}'), security_context=ctx,
    )
    assert result["blocked"] is True
    assert backend.calls == []


@pytest.mark.asyncio
async def test_exact_approval_runs_control_tool_in_unarmed_run(backend):
    """The sealed approval for a desktop action must execute even when no
    external untrusted context was ever seen (the generic exact-approval
    path used to require an armed run)."""
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action

    store = ToolApprovalStore()
    ctx = ToolRunSecurityContext()
    content = '{"x": 1, "y": 2}'
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id=ctx.run_id,
        tool_name="desktop_click", content=content, workspace="",
        external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("desktop_click", content),
    )
    approval = store.consume(pending.approval_id, decision="approve_task", owner="alice", session_id="s1")
    assert approval is not None
    desc, result = await execute_tool_block(
        ToolBlock("desktop_click", content), owner="alice", session_id="s1",
        security_context=ctx, exact_approval=approval,
    )
    assert result.get("exit_code") == 0, result
    assert backend.calls == [("click", 1, 2, "left")]
    # A second, un-approved call is gated again (ask_each).
    desc, result = await execute_tool_block(
        ToolBlock("desktop_click", content), owner="alice", session_id="s1", security_context=ctx,
    )
    assert result.get("blocked") is True


@pytest.mark.asyncio
async def test_exact_approval_for_other_tools_still_requires_armed_run():
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action

    store = ToolApprovalStore()
    ctx = ToolRunSecurityContext()
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id=ctx.run_id,
        tool_name="bash", content="echo hi", workspace="",
        external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("bash", "echo hi"),
    )
    approval = store.consume(pending.approval_id, decision="approve_task", owner="alice", session_id="s1")
    desc, result = await execute_tool_block(
        ToolBlock("bash", "echo hi"), owner="alice", session_id="s1",
        security_context=ctx, exact_approval=approval,
    )
    assert result["blocked"] is True
    assert result["policy"] == "exact_tool_approval"


# ── native function-call conversion ────────────────────────────────────────

def test_function_call_conversion_keeps_json_args():
    from src.tool_schemas import function_call_to_tool_block

    block = function_call_to_tool_block("desktop_click", '{"x": 10, "y": 20, "button": "right"}')
    assert block.tool_type == "desktop_click"
    assert json.loads(block.content) == {"x": 10, "y": 20, "button": "right"}
    block = function_call_to_tool_block("desktop_screenshot", "")
    assert block.tool_type == "desktop_screenshot"
