"""WP20 — layered image canvas compositor.

Real sqlite (own engine, ``test_creator_wp03.py``'s pattern), real PNG
fixtures via Pillow, real ``artifact_store``/``artifact_identity`` for every
occurrence the compositor reads or writes. No mocked PIL, no mocked
filesystem — the whole point of this module is that pixels come out right.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest
from PIL import Image, ImageOps
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_identity as identity
from src import artifact_store
from src import media_edit_projects as legacy
from src.contracts import ExecutionResult
from src.creator import canvas
from src.creator.documents import validate_content
from src.creator.errors import InvalidOperation
from src.creator.ops.model import Region
from src.creator.store import DocumentStore


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wp20.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def store_dir(tmp_path):
    d = str(tmp_path / "store")
    os.makedirs(d, exist_ok=True)
    return d


def _publish(tmp_path, store_dir, *, owner="alice", project_id="p1", filename="a.png",
            image: Image.Image = None, size=(40, 30), color=(200, 40, 40)):
    if image is None:
        image = Image.new("RGB", size, color)
    src_dir = tmp_path / f"src-{filename}-{owner}"
    src_dir.mkdir(exist_ok=True)
    path = src_dir / filename
    image.save(path)
    result = ExecutionResult.parse({
        "run_id": "run1", "backend": "docker_workspace", "status": "completed",
        "exit_code": 0, "artifact_filenames": [filename],
    })
    collected = artifact_store.collect(result, source_dir=str(src_dir), owner=owner,
                                       project_id=project_id, store_dir=store_dir)
    artifact_store.persist(collected.artifacts)
    return collected.artifacts[0]


def _doc_store(tmp_path) -> DocumentStore:
    return DocumentStore(db_path=str(tmp_path / "creator" / "creator_documents.sqlite3"))


# ── render: determinism + composition ──────────────────────────────────

def test_render_is_byte_identical_across_calls(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png", size=(20, 20), color=(10, 20, 30))
    top = _publish(tmp_path, store_dir, filename="top.png", size=(10, 10), color=(200, 0, 0))
    content = {
        "width": 20, "height": 20, "operation_semantics_version": 1,
        "base_asset_ref": base.id,
        "layers": [{
            "id": "l1", "kind": "raster", "visible": True, "opacity": 1.0,
            "offset": {"x": 5, "y": 5}, "asset_ref": top.id,
        }],
    }
    validate_content("canvas", content)
    a = canvas.render(content, "alice", store_dir=store_dir)
    b = canvas.render(content, "alice", store_dir=store_dir)
    assert a == b
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()

    img = Image.open(__import__("io").BytesIO(a))
    assert img.size == (20, 20)
    # The top layer's red is visible where it was placed.
    assert img.convert("RGB").getpixel((7, 7)) == (200, 0, 0)
    # Outside the top layer, the base color survives.
    assert img.convert("RGB").getpixel((1, 1)) == (10, 20, 30)


def test_hidden_layer_is_not_painted(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png", size=(10, 10), color=(0, 0, 0))
    top = _publish(tmp_path, store_dir, filename="top.png", size=(10, 10), color=(255, 255, 255))
    content = {
        "width": 10, "height": 10, "operation_semantics_version": 1,
        "base_asset_ref": base.id,
        "layers": [{
            "id": "l1", "kind": "raster", "visible": False, "opacity": 1.0,
            "offset": {"x": 0, "y": 0}, "asset_ref": top.id,
        }],
    }
    img = Image.open(__import__("io").BytesIO(canvas.render(content, "alice", store_dir=store_dir)))
    assert img.convert("RGB").getpixel((5, 5)) == (0, 0, 0)


def test_blend_modes_multiply_and_screen_differ_from_normal(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png", size=(10, 10), color=(200, 200, 200))
    top = _publish(tmp_path, store_dir, filename="top.png", size=(10, 10), color=(100, 100, 100))

    def rendered_pixel(blend_mode):
        content = {
            "width": 10, "height": 10, "operation_semantics_version": 1,
            "base_asset_ref": base.id,
            "layers": [{
                "id": "l1", "kind": "raster", "visible": True, "opacity": 1.0,
                "offset": {"x": 0, "y": 0}, "asset_ref": top.id, "blend_mode": blend_mode,
            }],
        }
        img = Image.open(__import__("io").BytesIO(canvas.render(content, "alice", store_dir=store_dir)))
        return img.convert("RGB").getpixel((5, 5))

    normal = rendered_pixel("normal")
    multiply = rendered_pixel("multiply")
    screen = rendered_pixel("screen")
    assert normal == (100, 100, 100)
    assert multiply == (int(200 * 100 / 255),) * 3
    assert screen == (255 - int((255 - 200) * (255 - 100) / 255),) * 3
    assert len({normal, multiply, screen}) == 3


def test_polygon_mask_restricts_layer_to_its_shape(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png", size=(20, 20), color=(0, 0, 0))
    top = _publish(tmp_path, store_dir, filename="top.png", size=(20, 20), color=(255, 0, 0))
    content = {
        "width": 20, "height": 20, "operation_semantics_version": 1,
        "base_asset_ref": base.id,
        "layers": [{
            "id": "l1", "kind": "raster", "visible": True, "opacity": 1.0,
            "offset": {"x": 0, "y": 0}, "asset_ref": top.id,
            # Left half only.
            "mask_polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 20}, {"x": 0, "y": 20}],
        }],
    }
    img = Image.open(__import__("io").BytesIO(canvas.render(content, "alice", store_dir=store_dir))).convert("RGB")
    assert img.getpixel((3, 10)) == (255, 0, 0)   # inside the polygon
    assert img.getpixel((15, 10)) == (0, 0, 0)    # outside the polygon: base shows through


def test_scale_and_rotate_layer_geometry(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png", size=(40, 40), color=(0, 0, 0))
    top = _publish(tmp_path, store_dir, filename="top.png", size=(10, 10), color=(0, 255, 0))
    content = {
        "width": 40, "height": 40, "operation_semantics_version": 1,
        "base_asset_ref": base.id,
        "layers": [{
            "id": "l1", "kind": "raster", "visible": True, "opacity": 1.0,
            "offset": {"x": 15, "y": 15}, "asset_ref": top.id, "scale": 2.0,
        }],
    }
    img = Image.open(__import__("io").BytesIO(canvas.render(content, "alice", store_dir=store_dir))).convert("RGB")
    # Scaled 2x (10x10 -> 20x20) centered on the original layer's center
    # (15+5, 15+5) = (20, 20): green must now reach further than 10px out.
    assert img.getpixel((20, 20)) == (0, 255, 0)
    assert img.getpixel((11, 20)) == (0, 255, 0)  # would be background at scale=1
    assert img.getpixel((0, 0)) == (0, 0, 0)


def test_unknown_blend_mode_is_rejected(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png")
    content = {
        "width": 40, "height": 30, "operation_semantics_version": 1,
        "base_asset_ref": base.id,
        "layers": [{
            "id": "l1", "kind": "raster", "visible": True, "opacity": 1.0,
            "offset": {"x": 0, "y": 0}, "asset_ref": base.id, "blend_mode": "difference",
        }],
    }
    with pytest.raises(InvalidOperation):
        canvas.render(content, "alice", store_dir=store_dir)


# ── region_to_source: EXIF orientation round trip ──────────────────────

def _oriented_png(path, orientation):
    """A small 4x6 RAW image with a unique color per pixel, saved with an
    EXIF orientation tag — same shape ``_oriented_load``/``region_to_source``
    are meant to handle."""
    raw = Image.new("RGB", (4, 6))
    for y in range(6):
        for x in range(4):
            raw.putpixel((x, y), (x * 50, y * 30, 77))
    exif = Image.Exif()
    exif[0x0112] = orientation
    raw.save(path, exif=exif)
    return raw


@pytest.mark.parametrize("orientation", [1, 6, 8])
def test_region_to_source_round_trips_exif_orientation(tmp_path, orientation):
    path = str(tmp_path / f"o{orientation}.png")
    _oriented_png(path, orientation)

    with Image.open(path) as f:
        oriented = ImageOps.exif_transpose(f).convert("RGB")
    raw = Image.open(path).convert("RGB")  # NOTE: without exif_transpose — the true raw pixels

    ow, oh = oriented.size
    # A region comfortably inside the oriented image.
    box_w, box_h = max(1, ow - 2), max(1, oh - 2)
    region = {
        "x": 1, "y": 1, "width": box_w, "height": box_h,
        "source_w": ow, "source_h": oh, "exif_orientation": orientation,
    }
    mapped = canvas.region_to_source(region)

    raw_box = (mapped["raw_x"], mapped["raw_y"],
              mapped["raw_x"] + mapped["raw_width"], mapped["raw_y"] + mapped["raw_height"])
    raw_crop = raw.crop(raw_box)
    # Re-apply the SAME orientation transform to just the raw crop (a real
    # engine re-decoding raw bytes would do exactly this) and it must equal
    # the direct crop of the already-oriented image at the original region.
    from PIL.Image import Exif as _Exif
    tagged = raw_crop.copy()
    exif = Image.Exif()
    exif[0x0112] = orientation
    import io as _io
    buf = _io.BytesIO()
    tagged.save(buf, format="PNG", exif=exif)
    buf.seek(0)
    with Image.open(buf) as reopened:
        re_oriented_crop = ImageOps.exif_transpose(reopened).convert("RGB")

    direct_crop = oriented.crop((1, 1, 1 + box_w, 1 + box_h))
    assert re_oriented_crop.size == direct_crop.size
    assert list(re_oriented_crop.getdata()) == list(direct_crop.getdata())


def test_region_to_source_identity_for_orientation_1():
    region = {"x": 2, "y": 3, "width": 5, "height": 4, "source_w": 20, "source_h": 20, "exif_orientation": 1}
    mapped = canvas.region_to_source(region)
    assert mapped["raw_x"] == 2 and mapped["raw_y"] == 3
    assert mapped["raw_width"] == 5 and mapped["raw_height"] == 4
    assert mapped["raw_source_w"] == 20 and mapped["raw_source_h"] == 20


def test_region_to_source_rejects_out_of_bounds_region():
    with pytest.raises(InvalidOperation):
        canvas.region_to_source({"x": 0, "y": 0, "width": 999, "height": 1, "source_w": 20, "source_h": 20})


# ── import_legacy: reads the legacy project, never writes it ───────────

def test_import_legacy_does_not_write_the_legacy_project(own_database, tmp_path, store_dir):
    legacy_dir = str(tmp_path / "legacy_projects")
    src_img = tmp_path / "photo.png"
    Image.new("RGB", (30, 20), (5, 10, 15)).save(src_img)
    record = legacy.create(str(src_img), owner="alice", directory=legacy_dir)

    import base64
    layer_buf = __import__("io").BytesIO()
    Image.new("RGBA", (10, 10), (255, 0, 0, 255)).save(layer_buf, format="PNG")
    layer_b64 = base64.b64encode(layer_buf.getvalue()).decode("ascii")
    legacy.add_layer(record["id"], owner="alice", image_b64=layer_b64, x=2, y=3,
                     label="sticker", directory=legacy_dir)

    project_path = os.path.join(legacy_dir, f"{record['id']}.json")
    before_bytes = open(project_path, "rb").read()
    before_mtime = os.path.getmtime(project_path)

    content = canvas.import_legacy(record["id"], "alice", "p1", directory=legacy_dir, store_dir=store_dir)

    after_bytes = open(project_path, "rb").read()
    after_mtime = os.path.getmtime(project_path)
    assert before_bytes == after_bytes, "import_legacy must not modify the legacy project file"
    assert before_mtime == after_mtime

    validate_content("canvas", content)
    assert content["width"] == 30 and content["height"] == 20
    assert len(content["layers"]) == 1
    layer = content["layers"][0]
    assert layer["offset"] == {"x": 2, "y": 3}
    assert layer["kind"] == "raster"
    # The base and the layer must both be real, readable occurrences now.
    identity.for_owner(content["base_asset_ref"], owner="alice")
    identity.for_owner(layer["asset_ref"], owner="alice")

    # And the imported document renders.
    png = canvas.render(content, "alice", store_dir=store_dir)
    img = Image.open(__import__("io").BytesIO(png)).convert("RGB")
    assert img.getpixel((5, 5)) == (255, 0, 0)   # sticker area
    assert img.getpixel((25, 15)) == (5, 10, 15)  # untouched base area


def test_import_legacy_preserves_masks(own_database, tmp_path, store_dir):
    legacy_dir = str(tmp_path / "legacy_projects2")
    src_img = tmp_path / "photo2.png"
    Image.new("RGB", (10, 10), (0, 0, 0)).save(src_img)
    record = legacy.create(str(src_img), owner="alice", directory=legacy_dir)

    import base64
    layer_buf = __import__("io").BytesIO()
    Image.new("RGBA", (10, 10), (255, 255, 255, 255)).save(layer_buf, format="PNG")
    mask_buf = __import__("io").BytesIO()
    Image.new("L", (10, 10), 128).save(mask_buf, format="PNG")
    legacy.add_layer(
        record["id"], owner="alice",
        image_b64=base64.b64encode(layer_buf.getvalue()).decode("ascii"),
        mask_b64=base64.b64encode(mask_buf.getvalue()).decode("ascii"),
        x=0, y=0, directory=legacy_dir,
    )
    content = canvas.import_legacy(record["id"], "alice", "p1", directory=legacy_dir, store_dir=store_dir)
    assert "mask_asset_ref" in content["layers"][0]


# ── export: publishes a new occurrence with derived_from ───────────────

def test_export_publishes_a_new_occurrence(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png", size=(12, 12), color=(9, 9, 9))
    doc_store = _doc_store(tmp_path)
    content = {
        "width": 12, "height": 12, "operation_semantics_version": 1,
        "base_asset_ref": base.id, "layers": [],
    }
    doc = doc_store.create("alice", "p1", "canvas", content)
    result = canvas.export(doc, "alice", fmt="png", store_dir=store_dir)
    assert result["created"] is True
    occ = identity.for_owner(result["occurrence_id"], owner="alice")
    assert base.id in occ.relations.derived_from
    assert occ.kind == "image"


def test_export_rejects_unknown_format(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png")
    doc_store = _doc_store(tmp_path)
    content = {"width": 40, "height": 30, "operation_semantics_version": 1,
              "base_asset_ref": base.id, "layers": []}
    doc = doc_store.create("alice", "p1", "canvas", content)
    with pytest.raises(InvalidOperation):
        canvas.export(doc, "alice", fmt="bmp", store_dir=store_dir)


# ── inpaint_request: mask rasterized in SOURCE (raw) coordinates ───────

def test_inpaint_request_builds_reference_and_mask_in_source_coords(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png", size=(20, 20), color=(30, 30, 30))
    doc_store = _doc_store(tmp_path)
    content = {
        "width": 20, "height": 20, "operation_semantics_version": 1,
        "base_asset_ref": base.id, "layers": [],
    }
    doc = doc_store.create("alice", "p1", "canvas", content)

    region = {"x": 5, "y": 5, "width": 8, "height": 8, "source_w": 20, "source_h": 20, "exif_orientation": 1}
    result = canvas.inpaint_request(doc, region, "add a hat", "alice", store_dir=store_dir)

    assert result["recipe_id"] == "inpaint"
    assert result["params"]["prompt"] == "add a hat"
    ref_id = result["params"]["reference_image"]
    mask_id = result["params"]["mask"]
    identity.for_owner(ref_id, owner="alice")
    mask_path = identity.path_for(mask_id, owner="alice", store_dir=store_dir)
    mask_img = Image.open(mask_path).convert("L")
    assert mask_img.size == (20, 20)
    # Inside the requested region: white (masked-in).
    assert mask_img.getpixel((9, 9)) == 255
    # Outside: black.
    assert mask_img.getpixel((1, 1)) == 0
    assert result["region"]["raw_x"] == 5 and result["region"]["raw_y"] == 5


def test_inpaint_request_rejects_empty_prompt(own_database, tmp_path, store_dir):
    base = _publish(tmp_path, store_dir, filename="base.png", size=(10, 10))
    doc_store = _doc_store(tmp_path)
    content = {"width": 10, "height": 10, "operation_semantics_version": 1,
              "base_asset_ref": base.id, "layers": []}
    doc = doc_store.create("alice", "p1", "canvas", content)
    region = {"x": 0, "y": 0, "width": 5, "height": 5, "source_w": 10, "source_h": 10}
    with pytest.raises(InvalidOperation):
        canvas.inpaint_request(doc, region, "", "alice", store_dir=store_dir)


def test_failed_proposal_does_not_touch_the_accepted_document(own_database, tmp_path, store_dir):
    """WP20 closure criterion: 'una propuesta fallida no modifica el
    lienzo aceptado'. inpaint_request is pure/read-derive-publish, never a
    write to the document itself — even a bad region raises before
    anything about the document is touched."""
    base = _publish(tmp_path, store_dir, filename="base.png", size=(10, 10))
    doc_store = _doc_store(tmp_path)
    content = {"width": 10, "height": 10, "operation_semantics_version": 1,
              "base_asset_ref": base.id, "layers": []}
    doc = doc_store.create("alice", "p1", "canvas", content)
    bad_region = {"x": 0, "y": 0, "width": 999, "height": 999, "source_w": 10, "source_h": 10}
    with pytest.raises(InvalidOperation):
        canvas.inpaint_request(doc, bad_region, "prompt", "alice", store_dir=store_dir)
    reloaded = doc_store.get("alice", doc.id)
    assert reloaded.revision == 1
    assert reloaded.content == content
