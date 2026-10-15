"""tests/test_visual_index_worker.py -- src/visual_index/worker.py (the stand-alone embedding worker), on its fake model."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image

from src.visual_index import embedder as emb_mod
from src.visual_index.subprocess_embedder import WORKER_SCRIPT

from tests.vi_helpers import BLUE, RED, page

spec = importlib.util.spec_from_file_location("visual_worker_under_test", WORKER_SCRIPT)
worker_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker_mod)


@pytest.fixture()
def fake(monkeypatch):
    monkeypatch.setenv("VISUAL_WORKER_FAKE", "1")
    return worker_mod.Worker(device="cpu")


def png(tmp_path, name, image):
    path = tmp_path / name
    image.save(path)
    return str(path)


def decode(reply, key="vectors"):
    arr = np.frombuffer(base64.b64decode(reply[key + "_b64"]), dtype="<f4")
    return arr.reshape(reply["shape"])


def test_the_worker_imports_nothing_heavy_and_nothing_of_faustus():
    source = open(WORKER_SCRIPT, encoding="utf-8").read()
    assert "from src" not in source and "import src" not in source
    top = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not any(("torch" in line or "transformers" in line or "numpy" in line or "PIL" in line) for line in top)


def test_model_constants_match_the_in_process_embedder():
    assert worker_mod.DEFAULT_MODEL == emb_mod.DEFAULT_MODEL
    assert worker_mod.DOCUMENT_INSTRUCTION == emb_mod.DOCUMENT_INSTRUCTION
    assert worker_mod.QUERY_INSTRUCTION == emb_mod.QUERY_INSTRUCTION


def test_embed_images_and_query_are_deterministic_unit_vectors(fake, tmp_path):
    a = png(tmp_path, "a.png", page([(300, RED)], 300))
    b = png(tmp_path, "b.png", page([(300, BLUE)], 300))
    reply = fake.handle({"op": "embed_images", "paths": [a, b, a]})
    arr = decode(reply)
    assert arr.shape == (3, reply["dim"]) and reply["dtype"] == "float32"
    assert np.allclose(np.linalg.norm(arr, axis=1), 1.0, atol=1e-5)
    assert np.allclose(arr[0], arr[2]) and not np.allclose(arr[0], arr[1])
    q = fake.handle({"op": "embed_query", "text": "red table"})
    assert decode(q, "vector").shape == (reply["dim"],)
    as_list = fake.handle({"op": "embed_query", "text": "red table", "format": "list"})
    assert np.allclose(as_list["vector"], decode(q, "vector"))


def test_info_load_and_errors_are_explained(fake, tmp_path):
    info = fake.handle({"op": "info"})
    assert info["loaded"] is False and info["fake"] is True
    assert fake.handle({"op": "info", "load": True})["loaded"] is True
    with pytest.raises(worker_mod.WorkerError) as e:
        fake.handle({"op": "embed_images", "paths": [str(tmp_path / "ghost.png")]})
    assert e.value.code == "unreadable_image"
    with pytest.raises(worker_mod.WorkerError) as e:
        fake.handle({"op": "embed_images", "paths": "x"})
    assert e.value.code == "invalid_request"
    with pytest.raises(worker_mod.WorkerError) as e:
        fake.handle({"op": "embed_query", "text": "  "})
    assert e.value.code == "invalid_request"
    with pytest.raises(worker_mod.WorkerError) as e:
        fake.handle({"op": "dance"})
    assert e.value.code == "invalid_request"


def run_worker(args, lines, env_extra=None, timeout=60):
    env = {**os.environ, "VISUAL_WORKER_FAKE": "1", "PYTHONUNBUFFERED": "1", **(env_extra or {})}
    return subprocess.run([sys.executable, "-u", WORKER_SCRIPT, *args], input=("\n".join(lines) + "\n").encode(),
                          capture_output=True, timeout=timeout, env=env)


def test_protocol_over_real_pipes_keeps_stdout_clean(tmp_path):
    a = png(tmp_path, "a.png", page([(300, RED)], 300))
    done = run_worker(["--device", "cpu"], [
        json.dumps({"op": "embed_images", "paths": [a], "id": 1}), "this is not json", json.dumps({"op": "nope", "id": 3}),
        json.dumps({"op": "embed_query", "text": "red", "id": 4}), json.dumps({"op": "close", "id": 5})],
        env_extra={"VISUAL_WORKER_FAKE_NOISE": "1"})
    assert done.returncode == 0
    lines = done.stdout.decode().splitlines()
    replies = [json.loads(line) for line in lines]                # every stdout line is JSON: no noise, no traces
    assert replies[0]["event"] == "ready" and replies[0]["fake"] is True
    by_id = {r.get("id"): r for r in replies[1:]}
    assert by_id[1]["ok"] and by_id[1]["shape"][0] == 1
    assert by_id[3]["ok"] is False and by_id[3]["code"] == "invalid_request"
    assert by_id[4]["ok"] and by_id[5]["closed"] is True
    assert any(r["ok"] is False and r["code"] == "invalid_request" and "JSON" in r["error"] for r in replies)
    assert b"noise from print" in done.stderr and b"C-level" in done.stderr         # the noise went to the log


def test_the_worker_exits_when_its_input_closes():
    done = run_worker([], [])
    assert done.returncode == 0 and json.loads(done.stdout.decode().splitlines()[0])["event"] == "ready"


def test_probe_reports_the_interpreter():
    done = subprocess.run([sys.executable, WORKER_SCRIPT, "--probe"], capture_output=True, timeout=60,
                          env={**os.environ, "VISUAL_WORKER_FAKE": "1", "VISUAL_WORKER_FAKE_GPUS": "4"})
    info = json.loads(done.stdout.decode().strip().splitlines()[-1])
    assert info["ok"] and info["device_count"] == 4 and info["cuda_available"] and info["transformers_ok"]


@pytest.mark.skipif(importlib.util.find_spec("torch") is not None, reason="this interpreter has torch")
def test_probe_without_torch_lists_what_is_missing():
    done = subprocess.run([sys.executable, WORKER_SCRIPT, "--probe"], capture_output=True, timeout=120,
                          env={k: v for k, v in os.environ.items() if k != "VISUAL_WORKER_FAKE"})
    info = json.loads(done.stdout.decode().strip().splitlines()[-1])
    assert "torch" in info["missing"] and info["device_count"] == 0
