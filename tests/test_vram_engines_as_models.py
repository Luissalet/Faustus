"""What a llama-server engine holds on each card counts as a model in the
VRAM bars, not as "other"."""
from routes.local_models_routes import _count_engines_as_models

MB = 1048576


def test_engine_share_moves_from_other_to_models():
    result = {"gpus": [
        {"index": 0, "used_bytes": 20000 * MB, "models_bytes": None, "other_bytes": 20000 * MB,
         "processes": [{"pid": 1, "label": "llama-server", "used_mb": 19000, "kind": "model"},
                       {"pid": 2, "label": "chrome.exe", "used_mb": 500, "kind": "other"}]},
        {"index": 1, "used_bytes": 3000 * MB, "models_bytes": 2000 * MB, "other_bytes": 1000 * MB,
         "processes": [{"pid": 3, "label": "Ollama", "used_mb": 2000, "kind": "model"}]},
    ]}
    _count_engines_as_models(result)
    first, second = result["gpus"]
    assert first["models_bytes"] == 19000 * MB and first["other_bytes"] == 1000 * MB
    assert second["models_bytes"] == 2000 * MB and second["other_bytes"] == 1000 * MB
    assert result["engines_bytes"] == 19000 * MB


def test_unknown_per_process_use_changes_nothing():
    result = {"gpus": [{"index": 0, "used_bytes": 5 * MB, "models_bytes": None, "other_bytes": 5 * MB,
                        "processes": [{"pid": 1, "label": "llama-server", "used_mb": None, "kind": "model"}]}]}
    _count_engines_as_models(result)
    assert result["gpus"][0]["models_bytes"] is None and "engines_bytes" not in result


def test_a_managed_engine_labelled_with_its_own_name_counts_as_a_model():
    """Live on the 7000: the 27B's managed engine is labelled "llama.cpp 27B
    (qwen3.8-27b-q8)", not "llama-server", and the bar said "models —"."""
    result = {"gpus": [{"used_bytes": 12 * 1048576 * 1024, "models_bytes": None, "processes": [
        {"pid": 7, "label": "llama.cpp 27B (qwen3.8-27b-q8)", "used_mb": 10240, "kind": "model", "engine": True},
        {"pid": 9, "label": "chrome.exe", "used_mb": 300, "kind": "other"},
    ]}]}
    _count_engines_as_models(result)
    assert result["engines_bytes"] == 10240 * 1048576
    assert result["gpus"][0]["models_bytes"] == 10240 * 1048576


def test_attribution_marks_engine_rows(monkeypatch):
    from routes import local_models_routes as lm
    from src import gpu_shared_memory
    monkeypatch.setattr(gpu_shared_memory, "compute_apps", lambda: [
        {"gpu_uuid": "GPU-A", "pid": 7, "process_name": "D:/llama.cpp/llama-server.exe", "used_mb": 100},
        {"gpu_uuid": "GPU-A", "pid": 8, "process_name": "ollama.exe", "used_mb": 50},
    ])
    monkeypatch.setattr(lm, "_engine_pids", lambda: {7: "llama.cpp 27B"})
    cards = [{"uuid": "GPU-A"}]
    lm._attribute_gpu_processes(cards)
    rows = {r["pid"]: r for r in cards[0]["processes"]}
    assert rows[7]["engine"] is True and rows[7]["label"] == "llama.cpp 27B"
    assert "engine" not in rows[8]
