"""OBJ-56: native tool descriptions stay under the footprint trim threshold.

The hybrid tool index keeps separate natural-language EXAMPLES
(`src/tool_index_examples.py`); trimming FUNCTION_TOOL_SCHEMAS descriptions
must not remove those examples.
"""
from __future__ import annotations

from src import tool_footprint as tf
from src.tool_index_examples import EXAMPLES
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def _native_rows():
    rows = []
    for entry in FUNCTION_TOOL_SCHEMAS:
        fn = entry.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        rows.append({
            "name": name,
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {},
            "executor": "native",
        })
    return rows


def test_no_native_description_exceeds_oversized_threshold():
    report = tf.footprint_report(_native_rows(), exposure_of=tf.builtin_exposure, top=50)
    assert report["oversized_descriptions"] == [], (
        "native descriptions over "
        f"{tf.OVERSIZED_DESCRIPTION_TOKENS} tokens: {report['oversized_descriptions']}"
    )


def test_index_examples_remain_for_formerly_oversized_tools():
    # Tools that were over the threshold before the OBJ-56 trim (measured
    # 2026-10-11 @ d95ec55): keep their hybrid-index examples.
    formerly = (
        "manage_tasks", "run_code", "blender_scene", "brain", "inspect_image",
        "review_candidature_mail", "verify_claim", "pdf_ops", "prior_art",
        "code_graph_drift", "plan_done", "agent_message", "run_report",
        "ui_control", "night_shift", "app_api", "expert_review",
    )
    missing = [n for n in formerly if not EXAMPLES.get(n)]
    assert missing == [], f"index examples missing after description trim: {missing}"


def test_ui_control_name_param_lists_built_in_theme_presets():
    """set_theme vs create_theme needs the preset names on the wire schema
    (Claude #301 r1 CHANGES): keep them on the `name` property, not only in
    the tool description, so the tool description can stay under 250 tokens.
    """
    entry = next(
        e for e in FUNCTION_TOOL_SCHEMAS
        if (e.get("function") or {}).get("name") == "ui_control"
    )
    name_prop = entry["function"]["parameters"]["properties"]["name"]["description"]
    presets = (
        "dark", "light", "midnight", "paper", "cyberpunk", "retrowave", "forest",
        "ocean", "ume", "copper", "terminal", "organs", "lavender", "graphite",
        "terracotta", "cute",
    )
    missing = [p for p in presets if p not in name_prop]
    assert missing == [], f"ui_control name param missing presets: {missing}"
    assert "library" in name_prop  # open_panel alias kept discoverable
