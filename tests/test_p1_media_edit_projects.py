"""MEDIA-03 — non-destructive image editing.

`src.media_edit_projects` keeps layers, masks and a history of operations as
DATA next to a hash of the original — never as pixels burned into the
source. The acceptance criterion is two claims:

* leaving the editor conserves masks and the draft — proven here by writing
  layers/history, then reading the project back in a FRESH call, the same
  way a process restart or a closed tab would see it;
* a PNG result never replaces the original without confirmation — proven by
  `export()` refusing the original path outright, and refusing any existing
  destination, unless told otherwise.

Revert-and-fail check (COMUN.md rule 5): with the `dest_abs == source_abs`
guard in `export()` removed (temporary edit, tested, reverted — see the
`test_p1_media_recipe_review.py` docstring for the same procedure applied
here), `test_export_refuses_to_silently_replace_the_original` fails: export
overwrites the source file with no confirmation flag at all.
"""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from src import media_edit_projects as projects


def _png_bytes(color=(255, 0, 0), size=(4, 4)):
    buf = io.BytesIO()
    Image.new("RGBA", size, color + (255,)).save(buf, format="PNG")
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture()
def source_image(tmp_path):
    path = tmp_path / "original.png"
    path.write_bytes(_png_bytes((10, 20, 30)))
    return path


# ── creating, and refusing what cannot be a project ────────────────────────

def test_create_records_the_original_hash(tmp_path, source_image):
    record = projects.create(str(source_image), owner="alice", directory=str(tmp_path))
    assert record["source_sha256"] == projects._sha256(str(source_image))
    assert record["layers"] == [] and record["history"] == []


def test_create_refuses_a_missing_source(tmp_path):
    with pytest.raises(projects.EditProjectError) as err:
        projects.create(str(tmp_path / "nope.png"), owner="alice", directory=str(tmp_path))
    assert err.value.code == "source_missing"


def test_create_requires_an_owner(tmp_path, source_image):
    with pytest.raises(projects.EditProjectError) as err:
        projects.create(str(source_image), owner="", directory=str(tmp_path))
    assert err.value.code == "no_owner"


# ── leaving the editor conserves masks and the draft ───────────────────────

def test_layers_and_history_survive_a_fresh_read(tmp_path, source_image):
    record = projects.create(str(source_image), owner="alice", directory=str(tmp_path))
    pid = record["id"]
    projects.add_layer(pid, owner="alice", image_b64=_b64(_png_bytes((0, 255, 0))),
                       x=1, y=2, label="patch", directory=str(tmp_path))
    projects.record_op(pid, owner="alice", op_type="crop",
                       params={"left": 0, "top": 0, "right": 3, "bottom": 3},
                       directory=str(tmp_path))

    # "Closing the editor" — nothing but a fresh get() call, no shared state.
    reloaded = projects.get(pid, owner="alice", directory=str(tmp_path))
    assert len(reloaded["layers"]) == 1
    assert reloaded["layers"][0]["label"] == "patch"
    assert reloaded["layers"][0]["x"] == 1 and reloaded["layers"][0]["y"] == 2
    op_types = [h["type"] for h in reloaded["history"]]
    assert "add_layer" in op_types and "crop" in op_types
    # The original file itself was never opened for writing.
    assert projects._sha256(str(source_image)) == record["source_sha256"]


def test_another_owner_cannot_read_or_edit_the_project(tmp_path, source_image):
    record = projects.create(str(source_image), owner="alice", directory=str(tmp_path))
    with pytest.raises(projects.EditProjectError) as err:
        projects.get(record["id"], owner="bob", directory=str(tmp_path))
    assert err.value.code == "not_found"
    with pytest.raises(projects.EditProjectError):
        projects.add_layer(record["id"], owner="bob", image_b64=_b64(_png_bytes()),
                           directory=str(tmp_path))


# ── export: explicit, and never a silent overwrite ─────────────────────────

def test_export_writes_a_new_file_and_leaves_the_original_untouched(tmp_path, source_image):
    record = projects.create(str(source_image), owner="alice", directory=str(tmp_path))
    projects.add_layer(record["id"], owner="alice", image_b64=_b64(_png_bytes((0, 255, 0))),
                       x=0, y=0, directory=str(tmp_path))
    before = source_image.read_bytes()
    dest = tmp_path / "flattened.png"
    out = projects.export(record["id"], str(dest), owner="alice", directory=str(tmp_path))
    assert out["ok"] is True and out["original_preserved"] is True
    assert dest.is_file()
    assert source_image.read_bytes() == before, "export must never touch the source file"


def test_export_refuses_an_existing_destination(tmp_path, source_image):
    record = projects.create(str(source_image), owner="alice", directory=str(tmp_path))
    dest = tmp_path / "already-here.png"
    dest.write_bytes(b"not touching this")
    with pytest.raises(projects.EditProjectError) as err:
        projects.export(record["id"], str(dest), owner="alice", directory=str(tmp_path))
    assert err.value.code == "destination_exists"
    assert dest.read_bytes() == b"not touching this"


def test_export_refuses_to_silently_replace_the_original(tmp_path, source_image):
    record = projects.create(str(source_image), owner="alice", directory=str(tmp_path))
    before = source_image.read_bytes()
    with pytest.raises(projects.EditProjectError) as err:
        projects.export(record["id"], str(source_image), owner="alice", directory=str(tmp_path))
    assert err.value.code == "would_overwrite_original"
    assert source_image.read_bytes() == before


def test_export_may_replace_the_original_with_explicit_confirmation(tmp_path, source_image):
    record = projects.create(str(source_image), owner="alice", directory=str(tmp_path))
    projects.add_layer(record["id"], owner="alice", image_b64=_b64(_png_bytes((5, 5, 5))),
                       directory=str(tmp_path))
    before = source_image.read_bytes()
    out = projects.export(record["id"], str(source_image), owner="alice",
                          confirm_overwrite_original=True, directory=str(tmp_path))
    assert out["ok"] is True
    assert source_image.read_bytes() != before, "the explicit confirmation should have taken effect"


# ── QA01 / 00_WP00_INVENTARIO.md §4: the order `export()` actually applies
#    crop/rotate/flip vs. layers was never verified beyond the docstring.
#    Pinned here with minimal 8×8 fixtures and `operation_semantics_version`
#    (see `src.media_edit_projects.OPERATION_SEMANTICS_VERSION`). The order
#    is NOT changed by this batch — only proven and versioned.

def _solid(color, size=(8, 8)):
    return _png_bytes(color, size=size)


def test_create_stamps_the_operation_semantics_version(tmp_path, source_image):
    record = projects.create(str(source_image), owner="alice", directory=str(tmp_path))
    assert record["operation_semantics_version"] == projects.OPERATION_SEMANTICS_VERSION


def test_export_applies_crop_before_layers_not_after(tmp_path):
    """The real, shipped order: `history` transforms run first against the
    original, THEN layers are painted on top of the already-transformed
    canvas — so a layer's (x, y) lands on the POST-crop canvas, not the
    original one.

    An 8×8 blue original; a 2×2 green layer pinned at (0, 0); a crop that
    keeps only the bottom-right 6×6 (`left=2, top=2`). If layers were
    composited BEFORE the crop, the green 2×2 square would sit entirely
    inside the region the crop then throws away, and the exported image
    would come out solid blue. Since crop actually runs first, the green
    square is painted at (0, 0) of the ALREADY-cropped 6×6 canvas and
    survives."""
    tmp_path = tmp_path
    source = tmp_path / "original.png"
    source.write_bytes(_solid((0, 0, 255)))  # 8x8 blue
    record = projects.create(str(source), owner="alice", directory=str(tmp_path))

    projects.add_layer(record["id"], owner="alice",
                       image_b64=_b64(_solid((0, 255, 0), size=(2, 2))),
                       x=0, y=0, directory=str(tmp_path))
    projects.record_op(record["id"], owner="alice", op_type="crop",
                       params={"left": 2, "top": 2, "right": 8, "bottom": 8},
                       directory=str(tmp_path))

    dest = tmp_path / "out.png"
    out = projects.export(record["id"], str(dest), owner="alice", directory=str(tmp_path))
    assert out["operation_semantics_version"] == projects.OPERATION_SEMANTICS_VERSION

    from PIL import Image
    exported = Image.open(dest).convert("RGBA")
    assert exported.size == (6, 6), "the crop must have run against the original"
    assert exported.getpixel((0, 0)) == (0, 255, 0, 255), (
        "the layer must be painted AFTER the crop, on the post-crop canvas — "
        "if it had been painted before, the crop would have thrown it away "
        "and this pixel would still be the original blue")
    assert exported.getpixel((5, 5)) == (0, 0, 255, 255), "everywhere the layer doesn't cover stays the (cropped) original"


def test_export_applies_multiple_transforms_in_the_order_they_were_recorded(tmp_path):
    """`history` entries run in the order they were appended, not sorted by
    type — crop-then-rotate and rotate-then-crop are different pictures, and
    this pins that `export()` respects the recorded sequence rather than
    applying, say, all rotates before all crops."""
    from PIL import Image

    source = tmp_path / "original.png"
    # Distinct corners so a rotation is observable: (8,8), top-left red.
    img = Image.new("RGBA", (8, 8), (255, 255, 255, 255))
    for x in range(4):
        for y in range(4):
            img.putpixel((x, y), (255, 0, 0, 255))  # top-left quadrant red
    img.save(source)

    record = projects.create(str(source), owner="alice", directory=str(tmp_path))
    # Crop to the top-left 4x4 (still all red), THEN rotate 90 — recorded in
    # that order.
    projects.record_op(record["id"], owner="alice", op_type="crop",
                       params={"left": 0, "top": 0, "right": 4, "bottom": 4},
                       directory=str(tmp_path))
    projects.record_op(record["id"], owner="alice", op_type="rotate",
                       params={"degrees": 90}, directory=str(tmp_path))
    dest = tmp_path / "crop-then-rotate.png"
    projects.export(record["id"], str(dest), owner="alice", directory=str(tmp_path))
    result = Image.open(dest).convert("RGBA")
    assert result.size == (4, 4), "crop ran on the 8x8 original before the rotate, per the recorded order"


def test_a_changed_original_stops_further_edits(tmp_path, source_image):
    record = projects.create(str(source_image), owner="alice", directory=str(tmp_path))
    source_image.write_bytes(_png_bytes((99, 99, 99)))  # something else touched it
    with pytest.raises(projects.EditProjectError) as err:
        projects.add_layer(record["id"], owner="alice", image_b64=_b64(_png_bytes()),
                           directory=str(tmp_path))
    assert err.value.code == "source_changed"
