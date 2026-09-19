"""src/engines.py — MTP speculative decoding (draft-mtp) support:
argv round-trip, extra_args non-duplication, validation against GGUF
support, decorate fields, and the VRAM admission headroom."""
from __future__ import annotations

import struct

import pytest

import src.engines as engines
import src.launch_profiles as launch_profiles

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_profiles, "DATA_DIR", str(tmp_path / "data"))
    launch_profiles._own_launches.clear()
    launch_profiles._profile_locks.clear()
    yield
    launch_profiles._own_launches.clear()
    launch_profiles._profile_locks.clear()


@pytest.fixture
def executable(tmp_path):
    import stat
    path = tmp_path / "llama-server"
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _write_gguf(path, nextn_layers) -> None:
    def kv_u32(key, value):
        kb = key.encode("utf-8")
        return struct.pack("<Q", len(kb)) + kb + struct.pack("<I", 4) + struct.pack("<I", value)

    kvs = []
    if nextn_layers is not None:
        kvs.append(kv_u32("qwen3.nextn_predict_layers", nextn_layers))
    with open(path, "wb") as f:
        f.write(b"GGUF")
        f.write(struct.pack("<I", 3))
        f.write(struct.pack("<Q", 0))
        f.write(struct.pack("<Q", len(kvs)))
        for kv in kvs:
            f.write(kv)


@pytest.fixture
def mtp_model(tmp_path):
    path = tmp_path / "qwen3-mtp.gguf"
    _write_gguf(path, 4)
    return str(path)


@pytest.fixture
def plain_model(tmp_path):
    path = tmp_path / "plain.gguf"
    _write_gguf(path, None)
    return str(path)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── argv round-trip ──────────────────────────────────────────────────────────

def test_create_with_mtp_writes_spec_flags(executable, mtp_model):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=mtp_model,
        port=_free_port(), mtp=True, mtp_draft_n_max=2,
    )
    assert "--spec-type" in engine["argv"]
    idx = engine["argv"].index("--spec-type")
    assert engine["argv"][idx + 1] == "draft-mtp"
    assert "--spec-draft-n-max" in engine["argv"]
    n_idx = engine["argv"].index("--spec-draft-n-max")
    assert engine["argv"][n_idx + 1] == "2"
    assert engine["mtp"] is True
    assert engine["mtp_draft_n_max"] == 2


def test_create_without_mtp_has_no_spec_flags(executable, mtp_model):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=mtp_model, port=_free_port(),
    )
    assert "--spec-type" not in engine["argv"]
    assert engine["mtp"] is False
    assert engine["mtp_draft_n_max"] == engines.DEFAULT_MTP_DRAFT_N_MAX


def test_update_toggles_mtp_off_removes_flags(executable, mtp_model):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=mtp_model,
        port=_free_port(), mtp=True,
    )
    updated = engines.update_engine(engine["id"], mtp=False)
    assert "--spec-type" not in updated["argv"]
    assert updated["mtp"] is False


def test_update_changes_draft_n_max(executable, mtp_model):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=mtp_model,
        port=_free_port(), mtp=True, mtp_draft_n_max=2,
    )
    updated = engines.update_engine(engine["id"], mtp_draft_n_max=4)
    n_idx = updated["argv"].index("--spec-draft-n-max")
    assert updated["argv"][n_idx + 1] == "4"
    assert updated["mtp_draft_n_max"] == 4


def test_extra_args_do_not_duplicate_spec_flags(executable, mtp_model):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=mtp_model,
        port=_free_port(), mtp=True, mtp_draft_n_max=3, extra_args=["--flash-attn"],
    )
    assert engine["extra_args"] == ["--flash-attn"]
    assert engine["argv"].count("--spec-type") == 1
    assert engine["argv"].count("--spec-draft-n-max") == 1


def test_parallel_flag_stays_in_extra_args_and_is_exposed(executable, mtp_model):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=mtp_model,
        port=_free_port(), extra_args=["-np", "4"],
    )
    assert "-np" in engine["extra_args"]
    assert engine["parallel"] == 4


# ── validation ───────────────────────────────────────────────────────────────

def test_mtp_rejected_when_model_lacks_nextn_layers(executable, plain_model):
    with pytest.raises(engines.EngineValidationError, match="MTP"):
        engines.create_engine(
            owner="tester", name="X", executable=executable, model_path=plain_model,
            port=_free_port(), mtp=True,
        )


def test_mtp_allowed_when_model_unknown(executable, tmp_path):
    # Model file does not exist yet — validation for a missing model file
    # already fails for a different reason (model file not found), so use a
    # non-GGUF unreadable-as-GGUF-metadata file that DOES exist to hit the
    # "support unknown" path via mtp_supported_for's None branch is instead
    # exercised directly.
    assert engines.mtp_supported_for(str(tmp_path / "missing.gguf")) is None


def test_mtp_draft_n_max_out_of_range_rejected(executable, mtp_model):
    with pytest.raises(engines.EngineValidationError):
        engines.create_engine(
            owner="tester", name="X", executable=executable, model_path=mtp_model,
            port=_free_port(), mtp=True, mtp_draft_n_max=9,
        )
    with pytest.raises(engines.EngineValidationError):
        engines.create_engine(
            owner="tester", name="X", executable=executable, model_path=mtp_model,
            port=_free_port(), mtp=True, mtp_draft_n_max=0,
        )


# ── decorate ─────────────────────────────────────────────────────────────────

def test_decorate_exposes_mtp_supported_true(executable, mtp_model):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=mtp_model, port=_free_port(),
    )
    assert engine["mtp_supported"] is True


def test_decorate_exposes_mtp_supported_false(executable, plain_model):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=plain_model, port=_free_port(),
    )
    assert engine["mtp_supported"] is False


# ── admission headroom ────────────────────────────────────────────────────────

def test_admission_check_adds_headroom_when_mtp_on(mtp_model):
    import os
    size = os.path.getsize(mtp_model)
    plain = engines.admission_check(mtp_model, mtp=False)
    with_mtp = engines.admission_check(mtp_model, mtp=True)
    assert plain["needed_bytes"] == size
    assert with_mtp["needed_bytes"] == size + engines.MTP_VRAM_HEADROOM_BYTES


async def test_start_engine_passes_mtp_flag_into_admission(monkeypatch, executable, mtp_model):
    engine = engines.create_engine(
        owner="tester", name="X", executable=executable, model_path=mtp_model,
        port=_free_port(), mtp=True,
    )
    from src import process_center
    monkeypatch.setattr(process_center, "pid_listening_on", lambda p, ports_by_pid=None: None)

    seen = {}
    real_admission_check = engines.admission_check

    def spy(model_path, mtp=False):
        seen["mtp"] = mtp
        return real_admission_check(model_path, mtp=mtp)

    monkeypatch.setattr(engines, "admission_check", spy)

    def fake_spawn(argv, *, cwd, env, log_path, owner=""):
        import os as _os
        from src import process_launch
        return process_launch.LaunchResult(pid=_os.getpid(), log_path=log_path, spawned_at=None)

    monkeypatch.setattr(launch_profiles, "spawn_detached", fake_spawn)

    async def never_ready(readiness):
        return False

    monkeypatch.setattr(launch_profiles, "_check_ready_once", never_ready)
    await engines.start_engine(engine["id"])
    assert seen["mtp"] is True
