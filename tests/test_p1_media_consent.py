"""MEDIA-06 — provenance and consent.

Two independent gaps this closes:

1. **Consent-gated recipes.** A recipe declared `requires_consent` (a face or
   voice clone) must never render for a subject nobody has registered
   consent for — `src.media_consent` is the registry, and
   `src.media_runs.plan()`/`.start()` enforce it before rendering or
   queueing. None of the four shipped templates declare it, so this is
   additive: nothing that worked before is gated now.

2. **Metadata minimization.** `routes/gallery/gallery_helpers.strip_location_exif`
   removes GPS EXIF on request; `routes/gallery/gallery_routes.gallery_download_zip`
   exposes it as an opt-in `redact_location` flag (default off, so every
   existing caller keeps getting byte-identical files).

Revert-and-fail check (COMUN.md rule 5): with the `_consent_gate(...)` calls
removed from `plan()`/`start()` (media_runs.py otherwise unchanged), the
"queues with no consent on file" tests below start passing where they should
fail — a consent-requiring recipe reaches the fake engine's queue for a
subject nobody registered. Restoring the calls fails it again. Verified by
hand (temp copy of media_runs.py, edited, tested, restored) — not left as a
second code path.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import media_consent
from src import media_runs
from src import media_workflows as mw
from tests.test_comfyui_backend import FakeComfy

SOURCE_TEMPLATE = mw.WORKFLOWS_DIR + "/image.reference-edit.v1.json"


def _cloning_template() -> dict:
    """A synthetic voice/face-clone recipe: same shape as a real template,
    with `requires_consent` pointed at its `reference` input."""
    payload = json.loads(open(SOURCE_TEMPLATE, encoding="utf-8").read())
    payload["id"] = "test.p1.media06.clone"
    payload["version"] = "1.0.0"
    payload["requires_consent"] = True
    payload["consent_subject_input"] = "reference"
    return payload


@pytest.fixture()
def library(tmp_path):
    (tmp_path / "reviews").mkdir()
    return tmp_path


def _write(library, payload, name="test.p1.media06.clone.v1.json"):
    (library / name).write_text(json.dumps(payload), encoding="utf-8")


def _approve(library, workflow):
    path = library / "reviews" / "approved_recipes.json"
    path.write_text(json.dumps({
        workflow.id: {"approved": [{
            "version": workflow.version, "fingerprint": workflow.fingerprint(),
            "node_types": sorted(mw.graph_node_types(workflow)),
        }]}
    }), encoding="utf-8")
    return str(path)


# ── the template field itself ──────────────────────────────────────────────

def test_requires_consent_must_name_a_real_declared_input():
    bad = _cloning_template()
    bad["consent_subject_input"] = "not_a_declared_input"
    with pytest.raises(mw.TemplateError) as err:
        mw.parse(bad)
    assert "consent_subject_input" in err.value.path


def test_shipped_templates_do_not_require_consent():
    """No regression: none of the real templates gate on this today."""
    for workflow in mw.catalogue()["workflows"]:
        assert workflow.requires_consent is False


# ── the registry ────────────────────────────────────────────────────────────

def test_register_then_has_consent(tmp_path):
    path = str(tmp_path / "consent.json")
    assert media_consent.has_consent("luis-voice", registry_path=path) is False
    out = media_consent.register("luis-voice", granted_by="luis@example.com", registry_path=path)
    assert out["ok"] is True
    assert media_consent.has_consent("luis-voice", registry_path=path) is True


def test_register_requires_a_grantor(tmp_path):
    path = str(tmp_path / "consent.json")
    out = media_consent.register("luis-voice", granted_by="", registry_path=path)
    assert out["ok"] is False and out["reason"] == "no_grantor"
    assert media_consent.has_consent("luis-voice", registry_path=path) is False


def test_revoke_stops_a_subject_from_having_consent(tmp_path):
    path = str(tmp_path / "consent.json")
    out = media_consent.register("luis-voice", granted_by="luis@example.com", registry_path=path)
    assert media_consent.has_consent("luis-voice", registry_path=path) is True
    revoked = media_consent.revoke(out["consent"]["id"], registry_path=path)
    assert revoked["ok"] is True
    assert media_consent.has_consent("luis-voice", registry_path=path) is False
    # ...and the history is not erased, only marked.
    history = media_consent.for_subject("luis-voice", registry_path=path)
    assert len(history) == 1 and history[0]["revoked_at"]


def test_revoking_an_unknown_id_is_reported_not_raised(tmp_path):
    path = str(tmp_path / "consent.json")
    assert media_consent.revoke("consent_nope", registry_path=path) == {"ok": False, "reason": "not_found"}


# ── enforced by plan()/start() ─────────────────────────────────────────────

@pytest.fixture()
def world(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "media.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    from src import artifact_store
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(artifact_store, "ARTIFACT_RUNS_DIR", str(tmp_path / "runs"))
    fake = FakeComfy()
    base_url = fake.start()
    monkeypatch.setenv("COMFYUI_URL", base_url)
    try:
        yield fake
    finally:
        fake.stop()
        engine.dispose()


def _prepare(library, monkeypatch):
    payload = _cloning_template()
    _write(library, payload)
    workflow = mw.parse(payload)
    monkeypatch.setattr(mw, "WORKFLOWS_DIR", str(library))
    monkeypatch.setattr(mw, "REVIEW_REGISTRY_PATH", _approve(library, workflow))
    return workflow


def test_plan_refuses_a_clone_with_no_registered_consent(world, library, monkeypatch, tmp_path):
    _prepare(library, monkeypatch)
    monkeypatch.setattr(media_consent, "CONSENT_FILE", str(tmp_path / "consent.json"))
    out = media_runs.plan("test.p1.media06.clone", {"prompt": "x", "reference": "luis-voice"},
                          check_engine=False)
    assert out["ok"] is False and out["reason"] == "consent_required"
    assert out["consent_subject_input"] == "reference"


def test_plan_allows_a_clone_once_consent_is_on_file(world, library, monkeypatch, tmp_path):
    _prepare(library, monkeypatch)
    consent_path = str(tmp_path / "consent.json")
    monkeypatch.setattr(media_consent, "CONSENT_FILE", consent_path)
    media_consent.register("luis-voice", granted_by="luis@example.com", registry_path=consent_path)
    out = media_runs.plan("test.p1.media06.clone", {"prompt": "x", "reference": "luis-voice"},
                          check_engine=False)
    assert out["ok"] is True


def test_start_refuses_a_clone_with_no_consent_and_never_reaches_the_engine(world, library, monkeypatch, tmp_path):
    _prepare(library, monkeypatch)
    monkeypatch.setattr(media_consent, "CONSENT_FILE", str(tmp_path / "consent.json"))
    out = media_runs.start("test.p1.media06.clone", {"prompt": "x", "reference": "luis-voice"})
    assert out["ok"] is False and out["reason"] == "consent_required"
    assert world.submitted == [], "a clone with no consent must never reach the engine"


def test_a_revoked_consent_refuses_again(world, library, monkeypatch, tmp_path):
    _prepare(library, monkeypatch)
    consent_path = str(tmp_path / "consent.json")
    monkeypatch.setattr(media_consent, "CONSENT_FILE", consent_path)
    granted = media_consent.register("luis-voice", granted_by="luis@example.com", registry_path=consent_path)
    assert media_runs.plan("test.p1.media06.clone", {"prompt": "x", "reference": "luis-voice"},
                           check_engine=False)["ok"] is True
    media_consent.revoke(granted["consent"]["id"], registry_path=consent_path)
    out = media_runs.plan("test.p1.media06.clone", {"prompt": "x", "reference": "luis-voice"},
                          check_engine=False)
    assert out["ok"] is False and out["reason"] == "consent_required"


# ── MEDIA-06 frontend requirement: optional EXIF location redaction ───────

def test_strip_location_exif_removes_gps_and_keeps_the_rest():
    from io import BytesIO
    from PIL import Image
    from routes.gallery.gallery_helpers import strip_location_exif

    img = Image.new("RGB", (4, 4), "red")
    exif = img.getexif()
    exif[0x8825] = {1: "N", 2: (40, 0, 0.0), 3: "W", 4: (74, 0, 0.0)}  # GPSInfo
    exif[271] = "TestCam"  # Make — should survive redaction
    buf = BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    original = buf.getvalue()

    redacted = strip_location_exif(original)
    assert redacted != original

    before = Image.open(BytesIO(original)).getexif()
    after = Image.open(BytesIO(redacted)).getexif()
    assert 0x8825 in before
    assert 0x8825 not in after
    assert after.get(271) == "TestCam"


def test_strip_location_exif_is_a_safe_no_op_without_gps():
    from io import BytesIO
    from PIL import Image
    from routes.gallery.gallery_helpers import strip_location_exif

    img = Image.new("RGB", (2, 2), "blue")
    buf = BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    assert strip_location_exif(data) == data


def test_strip_location_exif_never_raises_on_garbage():
    from routes.gallery.gallery_helpers import strip_location_exif
    assert strip_location_exif(b"not an image") == b"not an image"
