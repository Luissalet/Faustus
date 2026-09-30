"""Every model in memory, who loaded it, how much it holds and on which cards."""
from src import model_residency as mr

GB = 1024 ** 3


def _build(**kw):
    origins = kw.pop("origins", {})
    labels = kw.pop("labels", {})
    return mr.build(
        kw.pop("ollama", {"models": []}), kw.pop("runners", []), kw.pop("gpus", [{"index": 0}]),
        per_pid=kw.pop("per_pid", {}), listening=kw.pop("listening", {}), self_pid=1, profiles={},
        origin=lambda pid: origins.get(pid, {"kind": "unknown", "label": ""}),
        label=lambda pid: labels.get(pid, {"name": "", "hint": ""}),
    )


def test_two_runners_are_both_listed_with_their_owner_bytes_and_cards():
    out = _build(
        runners=[
            {"model": "qwen2.5-3b-helper", "root": "http://127.0.0.1:8082", "footprint_bytes": 2 * GB,
             "context_length": 16384, "endpoint_name": "helper 8082"},
            {"model": "qwen3.8-27b-q8", "root": "http://127.0.0.1:8081", "footprint_bytes": 28 * GB,
             "context_length": 131072, "generating": True, "endpoint_name": "llama.cpp (local)"},
        ],
        listening={8081: 100, 8082: 200},
        per_pid={100: {0: 16 * GB, 1: 16 * GB}, 200: {2: 3 * GB}},
        origins={100: {"kind": "external", "label": "Start-LlamaServer.ps1 (powershell.exe)"},
                 200: {"kind": "profile", "label": "Faustus: helper"}},
    )
    big, small = out["models"]
    assert big["model"] == "qwen3.8-27b-q8" and big["bytes"] == 32 * GB and big["measured"]
    assert [g["index"] for g in big["gpus"]] == [0, 1] and big["loaded_by"].startswith("Start-LlamaServer.ps1")
    assert big["weights_bytes"] == 28 * GB and big["generating"] is True and big["port"] == 8081
    assert small["model"] == "qwen2.5-3b-helper" and small["bytes"] == 3 * GB and small["gpus"] == [{"index": 2, "bytes": 3 * GB}]
    assert small["loaded_by_kind"] == "profile"
    assert out["others"] == []


def test_without_a_measurement_the_weights_are_shown_and_flagged():
    out = _build(runners=[{"model": "m", "root": "http://10.0.0.5:8081", "footprint_bytes": 5 * GB}],
                 listening={})
    row, = out["models"]
    assert row["bytes"] == 5 * GB and row["measured"] is False and row["pid"] is None and row["gpus"] == []


def test_ollama_models_keep_their_cards_and_are_loaded_by_ollama():
    out = _build(ollama={"base": "http://127.0.0.1:11434", "models": [
        {"name": "qwen3.5:9b", "size": 9 * GB, "size_vram": 8 * GB, "pid": 300,
         "per_gpu": [{"index": 3, "bytes": None}], "gpu_pct": 100, "context_length": 8192}]},
        per_pid={300: {3: 8 * GB}})
    row, = out["models"]
    assert row["loaded_by"] == "Ollama" and row["bytes"] == 8 * GB and row["gpus"] == [{"index": 3, "bytes": 8 * GB}]
    assert out["others"] == []  # the runner is the model, not another process


def test_other_gpu_processes_are_listed_and_small_ones_are_not():
    out = _build(per_pid={500: {0: 6 * GB}, 501: {0: 100 * 1024 * 1024}, 502: {1: None}},
                 labels={500: {"name": "python.exe", "hint": "ComfyUI/main.py"}},
                 origins={500: {"kind": "external", "label": "run_nvidia_gpu.bat (cmd.exe)"}})
    other, = out["others"]
    assert other["pid"] == 500 and other["bytes"] == 6 * GB and other["hint"] == "ComfyUI/main.py"
    assert other["loaded_by"].startswith("run_nvidia_gpu.bat")


def test_script_is_found_in_a_command_line():
    assert mr._script_of(["powershell.exe", "-File", r"D:\LocalAI\Start-LlamaServer.ps1"]) == "Start-LlamaServer.ps1"
    assert mr._script_of(["cmd.exe", "/c", "dir"]) == ""


def test_snapshot_never_raises(monkeypatch):
    mr.reset_cache()
    monkeypatch.setattr(mr, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = mr.snapshot({}, [], [])
    assert out["models"] == [] and "boom" in out["error"]
    mr.reset_cache()


def test_driver_contexts_on_other_cards_are_not_listed_as_cards():
    out = _build(runners=[{"model": "m", "root": "http://127.0.0.1:8081"}], listening={8081: 7},
                 per_pid={7: {0: 24576, 2: 11 * GB, 3: 10 * GB}})
    row, = out["models"]
    assert [g["index"] for g in row["gpus"]] == [2, 3]
    assert row["bytes"] == 21 * GB + 24576  # the total is still everything it holds
