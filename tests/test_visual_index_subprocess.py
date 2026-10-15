"""tests/test_visual_index_subprocess.py -- SubprocessEmbedder (visual_index_python) against fake workers: no GPU, no torch."""
from __future__ import annotations

import json
import os
import sys
import textwrap
import time

import numpy as np
import pytest

from src.visual_index import embedder as emb_mod
from src.visual_index import service as svc
from src.visual_index import subprocess_embedder as sub
from src.visual_index.embedder import EmbedderPool, EmbedderUnavailable, FakeEmbedder
from src.visual_index.store import VisualStore

from tests.vi_helpers import BLUE, RED, make_service, page, png_file


@pytest.fixture(autouse=True)
def _fresh_vision_cache():
    from src.visual_index.vision import clear_vision_cache
    clear_vision_cache()
    yield
    clear_vision_cache()


FAKE_WORKER = textwrap.dedent('''
    import argparse, json, os, sys
    import numpy as np
    ap = argparse.ArgumentParser()
    ap.add_argument("--model"); ap.add_argument("--cache-dir"); ap.add_argument("--device"); ap.add_argument("--batch")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    if a.probe:
        info = {"ok": True, "python": "3.x", "torch": "9.9", "cuda": "12.8", "cuda_available": True,
                "device_count": int(os.environ.get("FAKE_GPUS", "4")), "transformers": "5.16", "transformers_ok": True,
                "missing": [m for m in os.environ.get("FAKE_MISSING", "").split(",") if m]}
        print(json.dumps(info)); sys.exit(0)
    log = os.environ.get("FAKE_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"device": a.device, "visible": os.environ.get("CUDA_VISIBLE_DEVICES"),
                                "order": os.environ.get("CUDA_DEVICE_ORDER"), "pythonpath": os.environ.get("PYTHONPATH"),
                                "model": a.model, "cache": a.cache_dir, "pid": os.getpid()}) + "\\n")
    DIM = 8
    def vec(seed):
        rng = np.random.RandomState(abs(hash_of(seed)) % (2**31)); v = rng.rand(DIM).astype("<f4"); return v / np.linalg.norm(v)
    def hash_of(s):
        h = 0
        for ch in str(s): h = (h * 131 + ord(ch)) % 1000003
        return h
    def send(o):
        sys.__stdout__.write(json.dumps(o) + "\\n"); sys.__stdout__.flush()
    send({"ok": True, "event": "ready", "pid": os.getpid()})
    for line in sys.stdin:
        r = json.loads(line); rid = r.get("id"); op = r.get("op")
        if op == "load" or op == "info":
            send({"ok": True, "id": rid, "dim": DIM, "device": a.device, "loaded": True})
        elif op == "embed_images":
            marker = os.environ.get("FAKE_DIE_ONCE")
            if marker and not os.path.exists(marker):
                open(marker, "w").write("x"); os._exit(3)
            if os.environ.get("FAKE_ALWAYS_DIE"): os._exit(4)
            if os.environ.get("FAKE_HANG"):
                import time; time.sleep(60)
            import base64
            rows = [vec(os.path.getsize(p)) for p in r["paths"]]
            for p in r["paths"]: assert os.path.isfile(p)
            arr = np.stack(rows).astype("<f4")
            send({"ok": True, "id": rid, "dim": DIM, "shape": list(arr.shape), "vectors_b64": base64.b64encode(arr.tobytes()).decode()})
        elif op == "embed_query":
            import base64
            arr = vec(r["text"]).astype("<f4")
            send({"ok": True, "id": rid, "dim": DIM, "shape": [DIM], "vector_b64": base64.b64encode(arr.tobytes()).decode()})
        elif op == "close":
            send({"ok": True, "id": rid, "closed": True}); break
        else:
            send({"ok": False, "id": rid, "error": "bad op", "code": "invalid_request"})
''')


class Lease:
    def __init__(self, gpu, log):
        self.gpu, self.log = gpu, log

    def __enter__(self):
        self.log.append(("enter", self.gpu))
        return self

    def __exit__(self, *a):
        self.log.append(("exit", self.gpu))

    via = "test"
    warning = ""


@pytest.fixture()
def fake_script(tmp_path):
    path = tmp_path / "fake_worker.py"
    path.write_text(FAKE_WORKER, encoding="utf-8")
    sub.clear_probe_cache()
    return str(path)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    log = tmp_path / "launches.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("PYTHONPATH", "/should/not/leak")
    return log


def launches(log):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def make(fake_script, tmp_path, leases=None, gpu=2, **kw):
    leases = [] if leases is None else leases
    factory = (lambda vram, purpose, owner, gpus, timeout: Lease(gpu, leases)) if gpu is not None else None
    return sub.SubprocessEmbedder(sys.executable, script=fake_script, directory=str(tmp_path / "models"), lease_factory=factory,
                                  request_timeout=kw.pop("request_timeout", 30), **kw), leases


def image(color):
    return page([(60, color)], 60)


def test_start_embed_and_isolate_the_card(fake_script, tmp_path, env):
    e, leases = make(fake_script, tmp_path)
    vecs = e.embed_images([image(RED), image(BLUE)])
    assert vecs.shape == (2, 8) and vecs.dtype == np.float32 and e.dim == 8
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5)
    q = e.embed_query("red table")
    assert q.shape == (8,) and np.allclose(q, e.embed_query("red table"))
    (launch,) = launches(env)                                          # one start serves every call
    assert launch["visible"] == "2" and launch["order"] == "PCI_BUS_ID" and launch["device"] == "cuda:0"
    assert launch["pythonpath"] is None                                # the Faustus environment's variables are not passed on
    assert launch["cache"] == str(tmp_path / "models")
    status = e.status()
    assert status["mode"] == "subprocess" and status["loaded"] and status["device"] == "cuda:2" and status["gpu"] == 2
    assert status["worker_device"] == "cuda:0" and status["python"] == sys.executable
    assert leases == [("enter", 2)]
    e.close()
    assert leases == [("enter", 2), ("exit", 2)] and not e.status()["loaded"] and e.status()["device"] == ""
    e.close()                                                          # twice is fine
    assert leases.count(("exit", 2)) == 1


def test_no_free_card_means_cpu_in_the_worker(fake_script, tmp_path, env):
    def refuse(*a):
        raise RuntimeError("busy")

    e = sub.SubprocessEmbedder(sys.executable, script=fake_script, directory=str(tmp_path / "m"), lease_factory=refuse,
                               request_timeout=30)
    e.embed_query("x")
    (launch,) = launches(env)
    assert launch["device"] == "cpu" and launch["visible"] == ""
    assert e.status()["device"] == "cpu" and "busy" in e.status()["device_reason"]
    e.close()


def test_never_gpu_zero_or_one_even_if_the_arbiter_offers_them(fake_script, tmp_path, env):
    e, leases = make(fake_script, tmp_path, gpu=1)
    e.embed_query("x")
    (launch,) = launches(env)
    assert launch["device"] == "cpu" and launch["visible"] == ""
    assert leases == [("enter", 1), ("exit", 1)]                        # given straight back
    e.close()


def test_a_machine_without_the_allowed_cards_uses_the_cpu(fake_script, tmp_path, env, monkeypatch):
    monkeypatch.setenv("FAKE_GPUS", "2")                                # cards 0 and 1 only: both reserved
    e, leases = make(fake_script, tmp_path)
    e.embed_query("x")
    assert launches(env)[0]["device"] == "cpu" and leases == []
    e.close()


def test_the_worker_is_restarted_when_it_dies(fake_script, tmp_path, env, monkeypatch):
    monkeypatch.setenv("FAKE_DIE_ONCE", str(tmp_path / "died"))
    e, leases = make(fake_script, tmp_path)
    vecs = e.embed_images([image(RED)])                                 # the first worker dies mid-request; the second answers
    assert vecs.shape == (1, 8)
    assert len(launches(env)) == 2 and e.restarts == 1
    assert leases == [("enter", 2)]                                     # the same lease keeps covering the new worker
    pid = e.status()["worker_pid"]
    os.kill(pid, 9)                                                     # killed between calls
    time.sleep(0.3)
    assert e.embed_query("again").shape == (8,) and len(launches(env)) == 3
    e.close()


def test_a_worker_that_always_dies_gives_a_clear_error(fake_script, tmp_path, env, monkeypatch):
    monkeypatch.setenv("FAKE_ALWAYS_DIE", "1")
    e, leases = make(fake_script, tmp_path)
    with pytest.raises(EmbedderUnavailable) as err:
        e.embed_images([image(RED)])
    assert err.value.code == "worker_died"
    e.close()
    assert ("exit", 2) in leases


def test_a_worker_that_hangs_is_stopped_after_the_timeout(fake_script, tmp_path, env, monkeypatch):
    monkeypatch.setenv("FAKE_HANG", "1")
    e, _ = make(fake_script, tmp_path, request_timeout=1.5)
    with pytest.raises(EmbedderUnavailable) as err:
        e.embed_images([image(RED)])
    assert err.value.code == "worker_timeout" and not e.status()["worker_alive"]
    monkeypatch.delenv("FAKE_HANG")
    assert e.embed_images([image(RED)]).shape == (1, 8)                 # and it starts again on the next call
    e.close()


def test_a_python_that_does_not_exist(tmp_path):
    sub.clear_probe_cache()
    e = sub.SubprocessEmbedder(str(tmp_path / "nope" / "python.exe"), directory=str(tmp_path / "m"))
    with pytest.raises(EmbedderUnavailable) as err:
        e.embed_query("x")
    assert err.value.code == "python_not_found" and "nope" in str(err.value)
    st = e.status()
    assert st["python_exists"] is False and "not a file" in st["problem"] and st["loaded"] is False


def test_a_python_without_torch_names_what_to_install(fake_script, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MISSING", "torch,transformers")
    sub.clear_probe_cache()
    e = sub.SubprocessEmbedder(sys.executable, script=fake_script, directory=str(tmp_path / "m"))
    with pytest.raises(EmbedderUnavailable) as err:
        e.embed_images([image(RED)])
    msg = str(err.value)
    assert err.value.code == "dependencies_missing" and "torch, transformers" in msg and "pip install" in msg and "transformers>=4.57" in msg
    assert e.status(probe=True)["problem"].startswith("The Python at")


def test_an_old_transformers_is_reported(tmp_path):
    script = tmp_path / "old.py"
    script.write_text('import json\nprint(json.dumps({"ok": True, "transformers": "4.40.0", "transformers_ok": False, "missing": []}))\n')
    sub.clear_probe_cache()
    e = sub.SubprocessEmbedder(sys.executable, script=str(script), directory=str(tmp_path / "m"))
    with pytest.raises(EmbedderUnavailable) as err:
        e.embed_query("x")
    assert err.value.code == "dependencies_broken" and "4.40.0" in str(err.value)


def test_a_worker_error_reply_keeps_its_code(fake_script, tmp_path, env):
    e, _ = make(fake_script, tmp_path)
    e.embed_query("x")
    with pytest.raises(EmbedderUnavailable) as err:
        e._call({"op": "bogus"})
    assert err.value.code == "invalid_request"
    e.close()


def test_the_ttl_of_the_pool_closes_the_worker_and_the_lease(fake_script, tmp_path, env):
    e, leases = make(fake_script, tmp_path)
    pool = EmbedderPool(lambda: e, idle_s=0.4)
    with pool.use() as emb:
        emb.embed_query("x")
    assert e.status()["worker_alive"] and pool.loaded()
    time.sleep(1.2)
    assert not pool.loaded() and not e.status()["worker_alive"] and leases == [("enter", 2), ("exit", 2)]
    with pool.use() as emb:                                             # used again: a fresh start
        emb.embed_query("y")
    pool.unload()


def test_the_real_worker_in_fake_mode_end_to_end(tmp_path, monkeypatch):
    """The actual worker.py, started by SubprocessEmbedder through a service: add, search, status, close."""
    sub.clear_probe_cache()
    monkeypatch.setenv("VISUAL_WORKER_FAKE", "1")
    monkeypatch.setenv("VISUAL_WORKER_FAKE_GPUS", "4")
    leases = []
    e = sub.SubprocessEmbedder(sys.executable, directory=str(tmp_path / "m"),
                               lease_factory=lambda *a: Lease(3, leases), request_timeout=60)
    pool = EmbedderPool(lambda: e, idle_s=0)
    service = svc.VisualIndexService(store=VisualStore(str(tmp_path / "vi")), pool=pool, resolve=lambda p: p)
    red = png_file(tmp_path, "r.png", page([(300, RED)], 300))
    blue = png_file(tmp_path, "b.png", page([(300, BLUE)], 300))
    out = service.add("ada", [red, blue])
    assert all(r["ok"] and r["added"] >= 1 for r in out["results"]), out
    assert leases == [("enter", 3), ("exit", 3)]                       # idle 0: closed right after the call
    again = service.add("ada", [red])["results"][0]
    assert again["unchanged"] is True
    hit = service.search("ada", "x", k=2)
    assert len(hit["results"]) == 2
    status = service.status("ada")
    assert status["embedder"]["mode"] == "custom"                     # a pool with a test factory and nothing resident


def test_get_embedder_follows_the_setting(monkeypatch, tmp_path):
    from src import settings

    values = {"visual_index_python": ""}
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: values.get(key, default))
    assert isinstance(emb_mod.get_embedder(), emb_mod.Qwen3VLEmbedder)
    values["visual_index_python"] = f'"{sys.executable}"'              # quotes pasted from Explorer are tolerated
    got = emb_mod.get_embedder()
    assert isinstance(got, sub.SubprocessEmbedder) and got.python == sys.executable
    values["visual_index_python"] = os.path.dirname(sys.executable)     # a folder resolves to the interpreter in it
    assert sub.resolve_python(values["visual_index_python"]).startswith(os.path.dirname(sys.executable))


def test_pool_rebuilds_when_the_setting_changes(monkeypatch, tmp_path):
    from src import settings

    values = {"visual_index_python": ""}
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: values.get(key, default))
    pool = EmbedderPool(idle_s=1000)
    with pool.use() as first:
        assert first.mode == "in_process"
    values["visual_index_python"] = sys.executable
    with pool.use() as second:
        assert second.mode == "subprocess"
    pool.unload()


def test_status_reports_the_resident_embedder_and_its_device(monkeypatch):
    """In-process status used to build a fresh embedder, so `device` was always empty; it must read the resident one."""
    from src.visual_index.embedder import DeviceChoice, Qwen3VLEmbedder

    resident = Qwen3VLEmbedder()
    resident._model = object()
    resident._choice = DeviceChoice("cpu", reason="test")
    pool = EmbedderPool(lambda: resident, idle_s=1000)
    pool._factory = None                                               # behave like the real pool
    pool._embedder = resident
    st = emb_mod.embedder_status(pool)
    assert st["loaded"] is True and st["device"] == "cpu" and st["mode"] == "in_process"
    resident._model = None
    resident._choice = None


def test_status_of_a_fresh_pool_says_not_loaded():
    pool = EmbedderPool(None, idle_s=1000)
    st = emb_mod.embedder_status(pool)
    assert st["mode"] in ("in_process", "subprocess") and st["loaded"] is False


def test_repeating_add_on_a_web_page_reports_unchanged(tmp_path):
    from tests.vi_helpers import fake_web

    shot = page([(1024, RED), (1024, BLUE)], 875)
    service, emb = make_service(tmp_path, renderers=fake_web({"https://example.org/p": [(0, shot)]}))
    first = service.add("ada", ["https://example.org/p"])["results"][0]
    again = service.add("ada", ["https://example.org/p"])["results"][0]
    assert first["unchanged"] is False and again["unchanged"] is True and again["added"] == 0 and emb.image_calls == 2


def test_status_tool_and_route(tmp_path, monkeypatch):
    import asyncio

    from src.agent_tools import visual_index_tool as vit

    service, _ = make_service(tmp_path)
    previous = svc.set_service_for_tests(service)
    try:
        out = asyncio.run(vit.VisualIndexListTool().execute(json.dumps({"status": True}), {"owner": "ada"}))
        assert out["exit_code"] == 0 and "Embedder" in out["output"] and out["status"]["embedder"]["mode"] == "custom"
        bad = asyncio.run(vit.VisualIndexListTool().execute(json.dumps({"bogus": 1}), {"owner": "ada"}))
        assert bad["error_code"] == "invalid_arguments"
    finally:
        svc.set_service_for_tests(previous)
