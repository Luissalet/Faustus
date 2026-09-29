"""Exercise the live screenshot consumer copied verbatim from agent_loop."""
import ast
import base64
from copy import deepcopy
from pathlib import Path
import textwrap

import pytest
from PIL import Image

from src.tool_images import screenshot_data_url


@pytest.fixture
def png_payload(tmp_path):
    image_path = tmp_path / "synthetic.png"
    Image.new("RGB", (2, 3), (20, 40, 60)).save(image_path)
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


@pytest.fixture
def live_screenshot_consumer():
    path = Path(__file__).resolve().parents[1] / "src" / "agent_loop.py"
    source = path.read_text(encoding="utf-8")
    start = source.index("            # Forward screenshots from browser tools")
    end = source.index("            # Live browser view", start)
    # No replacement, injected statements, line-number assumptions, or imports
    # of the whole agent: compile the actual consumer between its comments.
    block = textwrap.dedent(source[start:end])
    tree = ast.parse("\n" * source[:start].count("\n") + block)
    assert any(
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "tool_output_data"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "screenshot"
        for node in ast.walk(tree)
    ), "The source extraction must still contain the live screenshot consumer"
    code = compile(tree, str(path), "exec")

    def consume(result):
        namespace = {
            "result": result,
            "tool_output_data": {"tool": "synthetic_image"},
            "screenshot_data_url": screenshot_data_url,
        }
        exec(code, namespace)
        return namespace["tool_output_data"]

    return consume


@pytest.mark.parametrize("shape", ["canonical", "mime_alias", "data_url", "data_url_no_mime", "invalid_first"])
def test_live_event_uses_normalized_image_without_mutation(shape, png_payload, live_screenshot_consumer):
    image = {"mimeType": "image/png", "data": png_payload}
    if shape == "mime_alias":
        image = {"mime_type": "image/png", "data": png_payload}
    elif shape == "data_url":
        image["data"] = f"data:image/png;base64,{png_payload}"
    elif shape == "data_url_no_mime":
        image = {"data": f"data:image/png;base64,{png_payload}"}
    result = {"images": [image], "caption": "Synthetic marks only"}
    if shape == "invalid_first":
        result["images"].insert(0, "not an image")
    original = deepcopy(result)
    expected = screenshot_data_url(result)
    assert expected == f"data:image/png;base64,{png_payload}"
    output = live_screenshot_consumer(result)
    assert output["screenshot"] == expected
    assert output["tool"] == "synthetic_image"
    assert result == original


@pytest.mark.parametrize("result", [{}, {"images": []}, {"images": [None]}, {"images": [{"data": ""}]}, {"images": "malformed"}])
def test_unusable_images_do_not_interrupt_result_processing(result, live_screenshot_consumer):
    original = deepcopy(result)
    output = live_screenshot_consumer(result)
    assert not output.get("screenshot")
    assert result == original
