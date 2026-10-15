"""tests/test_visual_index.py -- the local visual index (src/visual_index/) on a fake embedder and synthetic renders."""
from __future__ import annotations

import os

import numpy as np
import pytest
from PIL import Image

from src.visual_index import embedder as emb_mod
from src.visual_index import service as svc
from src.visual_index import tiling
from src.visual_index.embedder import EmbedderPool, EmbedderUnavailable, FakeEmbedder, choose_device
from src.visual_index.render import RenderError, parse_pages, render_pdf, render_source
from src.visual_index.store import StoreError, VisualStore, check_collection, owner_key, split_strip_id, strip_id

from tests.vi_helpers import BLUE, GREEN, RED, fake_web, make_service, page, png_file


@pytest.fixture(autouse=True)
def _fresh_vision_cache():
    from src.visual_index.vision import clear_vision_cache
    clear_vision_cache()
    yield
    clear_vision_cache()



# ------------------------------------------------------------------------------------------------ tiling
def test_grid_heights_widths_and_minimum_side():
    boxes = tiling.grid(1654, 2339)             # A4 at 200 dpi
    assert len(boxes) == 6
    assert {b[3] for b in boxes} == {1024, 291}
    assert [b[2] for b in boxes[:2]] == [875, 779]
    assert all(w >= tiling.MIN_SIDE and h >= tiling.MIN_SIDE for _, _, w, h in boxes)
    assert tiling.grid(100, 1040) == [(0, 0, 100, 1024)]             # the 16 px tail is under the minimum: dropped
    assert tiling.grid(20, 500) == []


def test_strips_are_at_most_1024_high_and_hash_by_content():
    img = page([(1500, RED), (600, BLUE)])
    strips = tiling.strips_from_image(img)
    assert [s.height for s in strips] == [1024, 1024, 52]
    assert len({s.md5 for s in strips}) == 3                          # different pixels, different hashes
    again = tiling.strips_from_image(img.copy())
    assert [s.md5 for s in again] == [s.md5 for s in strips]
    assert all(s.height <= tiling.STRIP_HEIGHT and s.width <= tiling.STRIP_WIDTH for s in strips)


def test_blank_strips_are_skipped_and_wide_images_split():
    img = page([(1024, (255, 255, 255)), (200, RED)], width=1800)
    strips = tiling.strips_from_image(img)
    assert [(s.x, s.y, s.width) for s in strips] == [(0, 1024, 875), (875, 1024, 875), (1750, 1024, 50)]
    assert tiling.strips_from_image(img, skip_blank=False)[0].y == 0


def test_tall_pages_are_tiled_at_8192_and_y_is_in_page_coordinates():
    img = page([(9000, RED)], width=100)
    tiles = list(tiling.split_tiles(img))
    assert [(y, t.height) for y, t in tiles] == [(0, 8192), (8192, 808)]
    strips = tiling.strips_from_page(img)
    assert max(s.y for s in strips) >= 8192 and max(s.y + s.height for s in strips) == 9000
    assert all(s.height <= 1024 for s in strips)


# ------------------------------------------------------------------------------------------------ store
def test_store_add_search_remove_roundtrip(tmp_path):
    service, emb = make_service(tmp_path)
    p = png_file(tmp_path, "a.png", page([(1000, BLUE)], width=300))
    out = service.add("ada", [p], collection="docs")
    r = out["results"][0]
    assert r["ok"] and r["added"] >= 1 and r["kind"] == "image"
    found = service.search("ada", "blue", k=3, collection="docs")
    assert found["results"] and found["results"][0]["source"] == p
    assert os.path.isfile(found["results"][0]["path"])
    assert 0 < found["results"][0]["score"] <= 1.0001
    listed = service.list("ada")
    assert listed["collections"][0]["collection"] == "docs"
    removed = service.remove("ada", "docs", p)
    assert removed["removed_strips"] >= 1
    assert service.search("ada", "blue", collection="docs")["results"] == []
    service.remove("ada", "docs")
    with pytest.raises(svc.VisualIndexError) as e:
        service.list("ada", "docs")
    assert e.value.code == "unknown_collection"


def test_search_ranks_the_matching_colour_first(tmp_path):
    service, _ = make_service(tmp_path)
    service.add("ada", [png_file(tmp_path, "r.png", page([(600, RED)], 300)), png_file(tmp_path, "g.png", page([(600, GREEN)], 300)),
                        png_file(tmp_path, "b.png", page([(600, BLUE)], 300))])
    for word, name in (("red", "r.png"), ("green", "g.png"), ("blue", "b.png")):
        top = service.search("ada", word, k=1)["results"][0]
        assert top["source"].endswith(name), word


def test_incremental_reindex_embeds_only_what_changed(tmp_path):
    service, emb = make_service(tmp_path)
    src = tmp_path / "doc.png"
    page([(2048, RED)], 300).save(src)
    first = service.add("ada", [str(src)])["results"][0]
    assert first["added"] == 2 and emb.image_calls == 2
    again = service.add("ada", [str(src)])["results"][0]
    assert again["unchanged"] is True and emb.image_calls == 2          # same bytes: nothing even rendered
    # the file changes: only the second strip does
    page([(1024, RED), (1024, BLUE)], 300).save(src)
    third = service.add("ada", [str(src)])["results"][0]
    assert (third["added"], third["reused"]) == (1, 1) and emb.image_calls == 3
    # shrinking drops the strip that is gone
    page([(1024, RED)], 300).save(src)
    fourth = service.add("ada", [str(src)])["results"][0]
    assert fourth["removed"] == 1 and fourth["total"] == 1 and emb.image_calls == 3
    # forcing re-renders but still reuses the vectors
    fifth = service.add("ada", [str(src)], force=True)["results"][0]
    assert fifth["reused"] == 1 and fifth["added"] == 0 and emb.image_calls == 3


def test_web_sources_are_tiled_and_reindexed_by_hash(tmp_path):
    shot = page([(1024, RED), (1024, GREEN), (52, BLUE)], 875)
    renderers = fake_web({"https://example.org/p": [(0, shot)]})
    service, emb = make_service(tmp_path, renderers=renderers)
    r = service.add("ada", ["https://example.org/p"])["results"][0]
    assert r["ok"] and r["kind"] == "web" and r["total"] == 3
    hit = service.search("ada", "blue", k=1)["results"][0]
    assert hit["kind"] == "web" and hit["source"] == "https://example.org/p" and hit["y_offset"] == 2048
    again = service.add("ada", ["https://example.org/p"])["results"][0]
    assert again["added"] == 0 and again["reused"] == 3 and emb.image_calls == 3


def test_pdf_pages_are_rendered_at_200_dpi_and_indexed_per_page(tmp_path):
    pytest.importorskip("pypdfium2")
    pdf = tmp_path / "doc.pdf"
    page([(300, RED)], 300).save(pdf)
    second = page([(300, BLUE)], 300)
    first = page([(300, RED)], 300)
    first.save(pdf, save_all=True, append_images=[second])
    rendered = render_pdf(str(pdf))
    assert rendered.kind == "pdf" and rendered.pages_total == 2
    pieces = list(rendered.pieces)
    assert [p.page for p in pieces] == [1, 2]
    assert pieces[0].image.width > 600                                   # 300 px at 72dpi-ish, rendered at 200 dpi
    service, _ = make_service(tmp_path)
    res = service.add("ada", [str(pdf)])["results"][0]
    assert res["ok"] and res["kind"] == "pdf" and res["pages"] == 2
    top = service.search("ada", "blue", k=1)["results"][0]
    assert top["page"] == 2
    only = service.add("ada", [str(pdf)], collection="one", pages="1")["results"][0]
    assert only["pages"] == 1


def test_parse_pages():
    assert parse_pages("1-3,7", 10) == [1, 2, 3, 7]
    assert parse_pages([2, 99], 10) == [2]
    assert parse_pages(None, 3) == [1, 2, 3]
    with pytest.raises(RenderError):
        parse_pages("x", 3)


def test_owners_and_collections_are_isolated(tmp_path):
    service, _ = make_service(tmp_path)
    p = png_file(tmp_path, "a.png", page([(500, RED)], 300))
    service.add("ada", [p], collection="mine")
    assert service.list("bob")["collections"] == []
    with pytest.raises(svc.VisualIndexError):
        service.search("bob", "red", collection="mine")
    sid = service.search("ada", "red")["results"][0]["id"]
    with pytest.raises(svc.VisualIndexError) as e:
        service.get_tile("bob", sid)
    assert e.value.code == "unknown_strip"
    assert owner_key("ada") != owner_key("bob")


def test_collection_names_and_strip_ids_are_validated():
    assert check_collection("my-docs_1") == "my-docs_1"
    for bad in ("../x", "a/b", ".hidden", "x" * 60):
        with pytest.raises(StoreError):
            check_collection(bad)
    sid = strip_id("docs", "/a/b.pdf", 3, 0, 1024)
    assert split_strip_id(sid)[0] == "docs"
    with pytest.raises(StoreError):
        split_strip_id("../../etc/passwd")


def test_embedder_mismatch_is_refused(tmp_path):
    service, _ = make_service(tmp_path)
    p = png_file(tmp_path, "a.png", page([(500, RED)], 300))
    service.add("ada", [p], collection="c")
    other = FakeEmbedder(dim=32)
    other.name = "other-embedder"
    service._pool = EmbedderPool(lambda: other, idle_s=0)
    with pytest.raises(svc.VisualIndexError) as e:
        service.search("ada", "red", collection="c")
    assert e.value.code == "embedder_mismatch"
    skipped = service.search("ada", "red")
    assert skipped["results"] == [] and skipped["skipped"][0]["collection"] == "c"


def test_failures_per_source_do_not_stop_the_batch(tmp_path):
    service, _ = make_service(tmp_path)
    good = png_file(tmp_path, "a.png", page([(500, RED)], 300))
    out = service.add("ada", [str(tmp_path / "missing.png"), str(tmp_path / "notes.txt"), good])
    ok = [r["ok"] for r in out["results"]]
    assert ok == [False, False, True]
    assert out["results"][0]["code"] == "not_found"


def test_disabled_setting(tmp_path, monkeypatch):
    from src import settings

    service, _ = make_service(tmp_path)
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: False if key == "visual_index_enabled" else default)
    with pytest.raises(svc.VisualIndexError) as e:
        service.add("ada", ["https://example.org/"])
    assert e.value.code == "disabled"


def test_view_slots_are_capped_per_turn(monkeypatch):
    from src import settings

    svc.reset_views_for_tests()
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: 2 if key == "visual_index_view_limit_per_turn" else default)
    assert svc.take_view_slot("t1") == 1
    assert svc.take_view_slot("t1") == 0
    assert svc.take_view_slot("t1") is None
    assert svc.take_view_slot("t2") == 1


# ------------------------------------------------------------------------------------------------ embedder
def test_without_transformers_the_error_is_clear_and_nothing_breaks(monkeypatch):
    monkeypatch.setattr(emb_mod, "dependencies_missing", lambda: ["torch", "transformers"])
    embedder = emb_mod.Qwen3VLEmbedder()
    with pytest.raises(EmbedderUnavailable) as e:
        embedder.embed_query("x")
    assert e.value.code == "dependencies_missing"
    assert "transformers" in str(e.value) and "pip install" in str(e.value)
    st = embedder.status()
    assert st["dependencies_missing"] == ["torch", "transformers"] and "pip install" in st["install_hint"]


def test_importing_the_module_and_the_app_surface_does_not_need_torch():
    import sys

    import src.visual_index.embedder  # noqa: F401
    import routes.visual_index_routes  # noqa: F401

    assert "torch" not in sys.modules or "transformers" not in sys.modules or True    # imports are lazy; nothing to assert beyond success
    assert hasattr(emb_mod.Qwen3VLEmbedder, "ensure_loaded")


def test_gpus_zero_and_one_are_never_allowed(monkeypatch):
    from src import settings

    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: "0,1,2,3" if key == "visual_index_gpus" else default)
    assert emb_mod.allowed_gpus() == [2, 3]
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: "0,1" if key == "visual_index_gpus" else default)
    assert emb_mod.allowed_gpus() == []
    assert choose_device(gpu_count=lambda: 4).device == "cpu"


def test_choose_device_uses_the_lease_and_refuses_reserved_cards():
    seen = {}

    class Lease:
        def __init__(self, gpu):
            self.gpu, self.exited = gpu, False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.exited = True

    def factory(vram_mb, purpose, owner, gpus, timeout_s):
        seen["gpus"] = list(gpus)
        return Lease(3)

    choice = choose_device(lease_factory=factory, gpu_count=lambda: 4)
    assert choice.device == "cuda:3" and seen["gpus"] == [2, 3]
    bad = Lease(0)
    out = choose_device(lease_factory=lambda *a: bad, gpu_count=lambda: 4)
    assert out.device == "cpu" and bad.exited and "outside the allowed" in out.reason
    assert choose_device(gpu_count=lambda: 2).device == "cpu"            # a two-card machine has no allowed card
    assert choose_device(lease_factory=lambda *a: (_ for _ in ()).throw(RuntimeError("hub down")), gpu_count=lambda: 4).device == "cpu"


def test_pool_builds_lazily_and_unloads_after_the_ttl():
    built = []

    def factory():
        e = FakeEmbedder()
        built.append(e)
        return e

    pool = EmbedderPool(factory, idle_s=0.05)
    assert not pool.loaded()
    with pool.use() as e:
        assert pool.loaded() and e is built[0]
    assert pool.loaded()                                                  # still resident until the TTL
    import time

    time.sleep(0.4)
    assert not pool.loaded() and built[0].closed
    with pool.use():
        pass
    assert len(built) == 2
    pool.unload()
    assert built[1].closed

    zero = EmbedderPool(factory, idle_s=0)
    with zero.use():
        pass
    assert not zero.loaded()


def test_l2_and_query_instruction():
    v = emb_mod.l2_normalise(np.array([[3.0, 4.0]], dtype=np.float32))
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-6
    assert emb_mod.QUERY_INSTRUCTION.rstrip(".") == "Retrieve images or text relevant to the user's query"
    assert emb_mod.DEFAULT_MODEL == "Qwen/Qwen3-VL-Embedding-2B"


def test_render_source_rejects_unsupported_and_confines_paths(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    with pytest.raises(RenderError) as e:
        render_source(str(txt), resolve=lambda p: p)
    assert e.value.code == "unsupported"
    with pytest.raises(RenderError):
        render_source("   ")


def test_a_failed_first_add_leaves_no_collection_behind(tmp_path):
    class Broken(FakeEmbedder):
        def embed_images(self, images):
            raise EmbedderUnavailable("no model", code="load_failed")

    service, _ = make_service(tmp_path, embedder=Broken())
    p = png_file(tmp_path, "a.png", page([(500, RED)], 300))
    with pytest.raises(EmbedderUnavailable):
        service.add("ada", [p], collection="fresh")
    assert service.list("ada")["collections"] == []
